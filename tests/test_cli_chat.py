"""Persistent CLI chat runtime."""

from __future__ import annotations

import pytest
import pytest_asyncio

from personal_agent.agent.agent import init_agent
from personal_agent.cli_chat import CliChatRuntime
from personal_agent.config import Settings
from personal_agent.db.database import Database
from personal_agent.gateway.compression_chain import CompressionChain
from personal_agent.gateway.session_store import SessionStore
from personal_agent.memory.base import MemoryProvider
from personal_agent.memory.manager import MemoryManager
from personal_agent.models.messages import NormalizedResponse
from personal_agent.llm.provider import ProviderProfile
from personal_agent.plugins.models import CommandEntry


class StaticMemory(MemoryProvider):
    async def prefetch(self, user_message: str) -> list[dict]:
        return []

    async def save(self, content: str) -> None:
        return None

    async def search(self, query: str) -> list[str]:
        return []

    async def load_all(self) -> list[str]:
        return []

    def get_system_prompt_text(self) -> str:
        return ""


class EchoTransport:
    def __init__(self):
        self.calls = 0

    async def call(self, messages, system_prompt="", tools=None, max_tokens=4096, stream=False):
        self.calls += 1
        last = messages[-1]["content"][0]["text"]
        return NormalizedResponse(
            text=f"echo:{last}",
            usage={"input_tokens": 2, "output_tokens": 3},
        )


class DummyPluginManager:
    def __init__(self):
        self.hooks = []
        self.commands = {}

    async def invoke_hook(self, name, *args, **kwargs):
        self.hooks.append(name)
        return None

    def get_command(self, name, *, scope="slash"):
        entry = self.commands.get(name)
        if entry is None:
            return None
        if entry.scope not in {scope, "both"}:
            return None
        return entry

    async def execute_command(self, name, **kwargs):
        entry = self.commands[name]
        value = entry.handler(**kwargs)
        if hasattr(value, "__await__"):
            value = await value
        return value


@pytest_asyncio.fixture
async def runtime(tmp_path):
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[],
        llm_provider="deepseek",
        llm_base_url="https://example.test",
    )
    db = Database(settings.agent_data_dir / "state.db")
    await db.initialize()
    chain = CompressionChain(settings.agent_data_dir / "compression_chain.json")
    store = SessionStore(db, settings.agent_data_dir, chain=chain)
    await store.initialize()
    transport = EchoTransport()
    provider = ProviderProfile(
        name="test",
        base_url="https://example.test",
        api_key="k",
        model="deepseek-chat",
        max_tokens=128,
        context_window=1000,
    )
    agent = init_agent(
        transport,
        provider,
        memory_manager=MemoryManager(StaticMemory()),
        system_prompt_template="system",
    )
    manager = DummyPluginManager()
    rt = CliChatRuntime(
        settings=settings,
        plugin_manager=manager,
        db=db,
        session_store=store,
        compression_chain=chain,
        memory_manager=MemoryManager(StaticMemory()),
        agent_cache={"cli:default:local": agent},
    )
    yield rt
    await rt.close()


@pytest.mark.asyncio
async def test_cli_chat_persists_multi_turn_history(runtime):
    first = await runtime.run_once("hello")
    second = await runtime.run_once("again")

    assert first == "echo:hello"
    assert second == "echo:again"
    session = await runtime.session_store.get_or_create(runtime.session_key, runtime.source)
    history = await runtime.session_store.load_history(session.session_id)
    texts = [msg["content"][0]["text"] for msg in history]
    assert texts == ["hello", "echo:hello", "again", "echo:again"]
    assert "on_session_selected" in runtime.plugin_manager.hooks


@pytest.mark.asyncio
async def test_cli_new_resets_session_and_agent(runtime):
    await runtime.run_once("hello")
    old_agent = runtime.agent_cache[runtime.session_key]

    result = await runtime.handle_command("/new")

    assert "会话已重置" in result
    assert runtime.session_key not in runtime.agent_cache
    session = await runtime.session_store.get_or_create(runtime.session_key, runtime.source)
    history = await runtime.session_store.load_history(session.session_id)
    assert history == []
    await runtime.session_store.save_transcript(session.session_id, [{
        "role": "user",
        "content": [{"type": "text", "text": "after reset"}],
    }])
    assert (await runtime.session_store.load_history(session.session_id))[0]["content"][0]["text"] == "after reset"
    assert old_agent is not runtime.agent_cache.get(runtime.session_key)


@pytest.mark.asyncio
async def test_cli_session_switch_list_usage_export_and_allow(runtime):
    await runtime.run_once("hello")

    switched = await runtime.handle_command("/session work")
    runtime.agent_cache[runtime.session_key] = next(iter(runtime.agent_cache.values()))
    listed = await runtime.handle_command("/session list")
    usage = await runtime.handle_command("/usage")
    allowed = await runtime.handle_command("/allow write")
    exported = await runtime.handle_command("/export")

    assert "cli:work:local" in switched
    assert "cli:work:local" in listed
    assert "上下文窗口" in usage
    assert "已授权 write" in allowed
    assert "已导出" in exported
    assert (runtime.settings.agent_data_dir / "exports" / "cli_work_local.jsonl").exists()
    agent = await runtime.get_or_create_agent()
    assert "write" in agent._destructive_allowed


@pytest.mark.asyncio
async def test_cli_memory_command_is_handled_locally(runtime, monkeypatch):
    called = False

    async def run_message(text):
        nonlocal called
        called = True
        return "model"

    async def list_entries(*, target="all"):
        return [{"id": "memory:1", "provider": "builtin", "target": target, "text": "remember cli"}]

    monkeypatch.setattr(runtime, "run_message", run_message)
    monkeypatch.setattr(runtime.memory_manager, "list_entries", list_entries)

    result = await runtime.handle_command("/memory list")

    assert result is not None
    assert "记忆列表" in result
    assert "remember cli" in result
    assert called is False


@pytest.mark.asyncio
async def test_cli_stop_reports_delegate_agent_count(runtime, monkeypatch):
    monkeypatch.setattr(
        "personal_agent.plugins.builtin.tools.builtin.delegate.stop_delegate_agents",
        lambda: 2,
    )

    stopped = await runtime.handle_command("/stop")

    assert stopped == "已停止。已请求停止 2 个子 agent。"
    assert all(agent._interrupt_requested for agent in runtime.agent_cache.values())


@pytest.mark.asyncio
async def test_cli_session_current_rename_and_delete(runtime):
    await runtime.run_once("hello")
    old_agent = runtime.agent_cache[runtime.session_key]

    current = await runtime.handle_command("/session current")
    renamed = await runtime.handle_command("/session rename renamed")
    listed = await runtime.handle_command("/session list")
    deleted = await runtime.handle_command("/session delete current")

    assert "session id" in current
    assert "cli:renamed:local" in renamed
    assert "cli:renamed:local" in listed
    assert "会话已删除: cli:renamed:local" in deleted
    assert runtime.session_key == "cli:default:local"
    assert "cli:renamed:local" not in runtime.agent_cache
    assert runtime.agent_cache.get("cli:default:local") is not old_agent


@pytest.mark.asyncio
async def test_cli_delete_named_session_without_switching(runtime):
    await runtime.switch_session("work")
    runtime.agent_cache[runtime.session_key] = next(iter(runtime.agent_cache.values()))
    await runtime.switch_session("default")

    deleted = await runtime.handle_command("/session delete work")

    assert "会话已删除: cli:work:local" in deleted
    assert runtime.session_key == "cli:default:local"
    assert "cli:work:local" not in runtime.agent_cache
    assert runtime.session_store.get("cli:work:local") is None


@pytest.mark.asyncio
async def test_cli_plugin_command_and_skill_command(runtime):
    async def plugin_handler(args="", **kwargs):
        return f"plugin:{args}:{kwargs['session_key']}"

    runtime.plugin_manager.commands["demo"] = CommandEntry(
        name="demo",
        description="demo",
        handler=plugin_handler,
    )
    runtime.plugin_manager.commands["local"] = CommandEntry(
        name="local",
        description="local only",
        handler=plugin_handler,
        scope="cli",
    )

    assert await runtime.handle_command("/demo hi") == "plugin:hi:cli:default:local"
    assert await runtime.handle_command("/local hi") == "plugin:hi:cli:default:local"


@pytest.mark.asyncio
async def test_cli_help_lists_visible_plugin_commands(runtime):
    async def plugin_handler(args="", **kwargs):
        return "ok"

    runtime.plugin_manager.commands["local"] = CommandEntry(
        name="local",
        description="local only",
        handler=plugin_handler,
        scope="cli",
        plugin_key="user/local",
    )

    help_text = await runtime.handle_command("/help")

    assert "插件命令:" in help_text
    assert "/local - local only (user/local)" in help_text


@pytest.mark.asyncio
async def test_cli_repl_exits_on_blank(runtime):
    outputs = []
    inputs = iter(["hello", ""])
    prompts = []

    def input_fn(prompt):
        prompts.append(prompt)
        return next(inputs)

    await runtime.repl(input_fn=input_fn, output_fn=outputs.append)

    assert "Personal Agent CLI" in outputs[0]
    assert "当前会话: cli:default:local" in outputs[0]
    assert prompts == ["cli:default:local >>> ", "cli:default:local >>> "]
    assert "echo:hello" in outputs


@pytest.mark.asyncio
async def test_cli_repl_prompt_tracks_session_switch(runtime):
    outputs = []
    inputs = iter(["/session work", ""])
    prompts = []

    def input_fn(prompt):
        prompts.append(prompt)
        return next(inputs)

    await runtime.repl(input_fn=input_fn, output_fn=outputs.append)

    assert prompts == [
        "cli:default:local >>> ",
        "cli:work:local >>> ",
    ]
    assert any("会话已切换: cli:work:local" in output for output in outputs)
