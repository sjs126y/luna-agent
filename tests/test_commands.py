"""Shared slash command service."""

from __future__ import annotations

import pytest

from personal_agent.commands.runtime import handle_slash_command
from personal_agent.config import Settings
from personal_agent.models.messages import SessionSource
from personal_agent.plugins.models import CommandEntry


class Agent:
    session_api_calls = 2
    session_prompt_tokens = 10
    session_completion_tokens = 5
    _last_skill_summaries = ""
    _last_skill_injection = ""
    _last_memory_injections = ""
    _tool_calls_this_turn = 0
    _max_tool_calls_per_turn = 20
    _destructive_allowed: set[str]
    _interrupt_requested = False
    _cached_system_prompt = "system"
    tools = []
    model = "deepseek-chat"
    _memory_manager = None

    class Provider:
        model = "deepseek-chat"
        context_window = 1000

    _provider = Provider()

    def __init__(self):
        self._destructive_allowed = set()


class PluginManager:
    def __init__(self):
        self.commands = {}

    def get_command(self, name, *, scope="slash"):
        entry = self.commands.get(name)
        if entry is None:
            return None
        if entry.scope not in {scope, "both"}:
            return None
        return entry

    async def execute_command(self, name, **kwargs):
        value = self.commands[name].handler(**kwargs)
        if hasattr(value, "__await__"):
            value = await value
        return value


class Runtime:
    def __init__(self, tmp_path):
        self.settings = Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
        self.plugin_manager = PluginManager()
        self._session_key = "cli:default:local"
        self.source = SessionSource(platform="cli", user_id="local", chat_id="default")
        self.agent = Agent()
        self.reset_called = False
        self.clear_called = False
        self.switched_to = ""
        self.renamed_to = ""
        self.deleted = None
        self.exported = False
        self.memory_deleted = None

    @property
    def session_key(self):
        return self._session_key

    async def get_agent(self):
        return self.agent

    async def reset_session(self):
        self.reset_called = True

    async def switch_session(self, name: str):
        self.switched_to = name
        self._session_key = f"cli:{name}:local"
        return f"会话已切换: {self._session_key}"

    async def list_sessions(self):
        return f"当前会话: {self._session_key}"

    async def current_session(self):
        return f"当前会话: {self._session_key}\nsession id: abc123\n消息数: 0"

    async def rename_session(self, name: str):
        self.renamed_to = name
        self._session_key = f"cli:{name}:local"
        return f"会话已重命名: {self._session_key}"

    async def delete_session(self, name: str | None = None):
        self.deleted = name
        return f"会话已删除: {name or self._session_key}"

    async def load_history(self):
        return [{
            "role": "user",
            "content": [{"type": "text", "text": "hello"}],
        }]

    async def export_session(self):
        self.exported = True
        return 1, "/tmp/export.jsonl"

    async def memory_report(self):
        return {
            "providers": {
                "builtin": {"provider": "FileMemoryProvider", "available": True, "entries": 1},
                "external": {"provider": "", "available": False, "entries": 0},
            },
            "last_errors": {},
        }

    async def memory_entries(self, *, target: str = "all"):
        return [{"id": "memory:1", "provider": "builtin", "target": "memory", "text": f"hello {target}"}]

    async def memory_search(self, query: str, *, target: str = "all"):
        return [{"id": "memory:1", "provider": "builtin", "target": "memory", "text": query}]

    async def memory_entry(self, identifier: str, *, target: str = "all"):
        if identifier == "memory:1":
            return {"id": identifier, "provider": "builtin", "target": target, "text": "hello"}
        return None

    async def memory_delete(self, identifier: str, *, target: str = "all"):
        self.memory_deleted = (identifier, target)
        return identifier == "memory:1"

    async def clear_agent(self):
        self.clear_called = True

    def plugin_command_kwargs(self, args: str):
        return {
            "args": args,
            "runtime": self,
            "session_key": self.session_key,
        }


@pytest.mark.asyncio
async def test_shared_command_core_session_usage_export_and_allow(tmp_path):
    runtime = Runtime(tmp_path)

    result = await handle_slash_command(runtime, "/new")
    assert result.handled
    assert runtime.reset_called
    assert runtime.clear_called

    result = await handle_slash_command(runtime, "/session work")
    assert result.response == "会话已切换: cli:work:local"

    result = await handle_slash_command(runtime, "/session list")
    assert "当前会话: cli:work:local" in result.response

    result = await handle_slash_command(runtime, "/session current")
    assert "session id" in result.response

    result = await handle_slash_command(runtime, "/session switch ops")
    assert result.response == "会话已切换: cli:ops:local"

    result = await handle_slash_command(runtime, "/session rename renamed")
    assert "会话已重命名" in result.response
    assert runtime.renamed_to == "renamed"

    result = await handle_slash_command(runtime, "/session delete current")
    assert "会话已删除" in result.response
    assert runtime.deleted is None

    result = await handle_slash_command(runtime, "/session delete old")
    assert "会话已删除" in result.response
    assert runtime.deleted == "old"

    result = await handle_slash_command(runtime, "/usage")
    assert "上下文窗口" in result.response

    result = await handle_slash_command(runtime, "/export")
    assert "已导出 1 条对话" in result.response
    assert runtime.exported

    result = await handle_slash_command(runtime, "/allow write")
    assert "已授权 write" in result.response
    assert "write" in runtime.agent._destructive_allowed

    result = await handle_slash_command(runtime, "/memory list")
    assert result.handled
    assert "记忆列表: 1 条" in result.response
    assert "memory:1" in result.response

    result = await handle_slash_command(runtime, "/memory search needle --target=memory")
    assert "记忆搜索结果: 1 条" in result.response
    assert "needle" in result.response

    result = await handle_slash_command(runtime, "/memory search needle -t memory")
    assert "needle memory" not in result.response

    result = await handle_slash_command(runtime, "/memory show memory:1")
    assert "记忆: memory:1" in result.response

    result = await handle_slash_command(runtime, "/memory delete memory:1 -t external")
    assert result.response == "已删除记忆: memory:1"
    assert runtime.memory_deleted == ("memory:1", "external")

    result = await handle_slash_command(runtime, "/memory doctor")
    assert "Memory 诊断" in result.response


@pytest.mark.asyncio
async def test_shared_command_stop_plugin_skill_and_unhandled(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)

    result = await handle_slash_command(runtime, "/stop")
    assert result.response == "已停止。"
    assert runtime.agent._interrupt_requested

    async def plugin_handler(args="", **kwargs):
        return f"plugin:{args}:{kwargs['session_key']}"

    runtime.plugin_manager.commands["demo"] = CommandEntry(
        name="demo",
        description="demo",
        handler=plugin_handler,
    )
    result = await handle_slash_command(runtime, "/demo hi")
    assert result.response == "plugin:hi:cli:default:local"

    runtime.plugin_command_scopes = ("cli", "slash")
    runtime.plugin_manager.commands["local"] = CommandEntry(
        name="local",
        description="local command",
        handler=plugin_handler,
        scope="cli",
    )
    result = await handle_slash_command(runtime, "/local only")
    assert result.response == "plugin:only:cli:default:local"

    result = await handle_slash_command(runtime, "/help")
    assert "/local - local command" in result.response
    assert "/demo - demo" in result.response

    from personal_agent.skills.registry import skill_registry

    original_load = skill_registry.load
    monkeypatch.setattr(skill_registry, "load", lambda name: "skill body")
    try:
        result = await handle_slash_command(runtime, "/python-expert fix it")
    finally:
        monkeypatch.setattr(skill_registry, "load", original_load)
    assert result.continue_text == "fix it"
    assert "[技能: python-expert]" in runtime.agent._pending_skill_injection

    monkeypatch.setattr(skill_registry, "load", lambda name: "")
    result = await handle_slash_command(runtime, "/unknown")
    assert not result.handled


@pytest.mark.asyncio
async def test_shared_command_stop_reports_delegate_agent_count(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    monkeypatch.setattr(
        "personal_agent.plugins.builtin.tools.builtin.delegate.stop_delegate_agents",
        lambda: 2,
    )

    result = await handle_slash_command(runtime, "/stop")

    assert result.response == "已停止。已请求停止 2 个子 agent。"
    assert runtime.agent._interrupt_requested


@pytest.mark.asyncio
async def test_shared_command_uses_exact_command_names(tmp_path):
    runtime = Runtime(tmp_path)

    result = await handle_slash_command(runtime, "/newsletter")

    assert not result.handled
    assert not runtime.reset_called


@pytest.mark.asyncio
async def test_shared_command_lists_shows_and_clears_agent_runs(tmp_path):
    from personal_agent.models.messages import NormalizedResponse
    from personal_agent.plugins.builtin.tools.builtin.delegate import (
        _delegate_task,
        reset_delegate,
        setup_delegate,
    )

    async def call_fn(messages, system_prompt, tools, max_tokens):
        return NormalizedResponse(text="agent result", usage={"input_tokens": 1, "output_tokens": 2})

    reset_delegate()
    setup_delegate(call_fn, tools=[], max_tokens=100)
    await _delegate_task("inspect", role="reviewer")
    runtime = Runtime(tmp_path)

    listed = await handle_slash_command(runtime, "/agents list")
    run_id = listed.response.splitlines()[1].split()[1]
    shown = await handle_slash_command(runtime, f"/agents show {run_id}")
    cleared = await handle_slash_command(runtime, "/agents clear")
    empty = await handle_slash_command(runtime, "/agent-runs")

    assert "子 agent 运行记录" in listed.response
    assert "reviewer" in listed.response
    assert "agent result" in shown.response
    assert "已清理 1 条" in cleared.response
    assert "暂无子 agent" in empty.response
    reset_delegate()
