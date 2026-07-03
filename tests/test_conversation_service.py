"""Shared conversation service behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

from personal_agent.config import Settings
from personal_agent.conversation import ConversationService
from personal_agent.db.database import Database
from personal_agent.gateway.compression_chain import CompressionChain
from personal_agent.gateway.session_store import SessionStore
from personal_agent.models.messages import SessionSource
from personal_agent.tools.registry import tool_registry


class PluginManager:
    def __init__(self):
        self.hooks = []

    async def invoke_hook(self, name, *args, **kwargs):
        self.hooks.append((name, kwargs))
        return None


class Agent:
    session_api_calls = 0
    session_prompt_tokens = 0
    session_completion_tokens = 0
    _last_skill_summaries = ""
    _last_skill_injection = ""
    _last_memory_injections = ""
    _tool_calls_this_turn = 0
    _max_tool_calls_per_turn = 20
    _cached_system_prompt = "system"
    tools = []
    model = "deepseek-chat"
    _memory_manager = None

    class Provider:
        model = "deepseek-chat"
        context_window = 1000

    _provider = Provider()

    def __init__(self, generation: int | None = None):
        self._tools_generation = tool_registry.generation if generation is None else generation
        self._interrupt_requested = False
        self._destructive_allowed = set()


class Ctx:
    def __init__(self, messages, *, was_compressed=False, should_review_memory=False):
        self.messages = messages
        self.was_compressed = was_compressed
        self.should_review_memory = should_review_memory


@pytest_asyncio.fixture
async def service(tmp_path):
    settings = Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
    db = Database(settings.agent_data_dir / "state.db")
    await db.initialize()
    chain = CompressionChain(settings.agent_data_dir / "compression_chain.json")
    chain.load()
    store = SessionStore(db, settings.agent_data_dir, chain=chain)
    await store.initialize()
    manager = PluginManager()
    svc = ConversationService(
        settings=settings,
        plugin_manager=manager,
        session_store=store,
        compression_chain=chain,
        memory_manager=None,
        agent_cache={"cli:default:local": Agent()},
    )
    yield svc, manager, db
    await db.close()


def _source() -> SessionSource:
    return SessionSource(platform="cli", user_id="local", chat_id="default", user_name="CLI")


def _messages(user_text="hello", assistant_text="echo:hello"):
    return [
        {"role": "user", "content": [{"type": "text", "text": user_text}]},
        {"role": "assistant", "content": [{"type": "text", "text": assistant_text}]},
    ]


@pytest.mark.asyncio
async def test_run_turn_persists_history_and_invokes_session_hook(service, monkeypatch):
    svc, manager, _db = service

    async def build_turn_context(agent, text, history):
        assert text == "hello"
        assert history == []
        return Ctx(_messages())

    async def run_conversation(agent, ctx):
        return {
            "final_response": "echo:hello",
            "messages": ctx.messages,
            "completed": True,
            "should_review_memory": True,
        }

    monkeypatch.setattr("personal_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("personal_agent.agent.loop.run_conversation", run_conversation)

    result = await svc.run_turn("cli:default:local", _source(), "hello")

    assert result.final_response == "echo:hello"
    assert result.completed is True
    assert result.should_review_memory is True
    assert manager.hooks[0] == ("on_session_selected", {"session_key": "cli:default:local"})
    session = await svc.session_store.get_or_create("cli:default:local", _source())
    history = await svc.session_store.load_history(session.session_id)
    assert [msg["content"][0]["text"] for msg in history] == ["hello", "echo:hello"]


@pytest.mark.asyncio
async def test_run_turn_events_collects_and_forwards_events(service, monkeypatch):
    from personal_agent.conversation.events import emit_event

    svc, _manager, _db = service
    forwarded = []

    class Sink:
        async def emit(self, event):
            forwarded.append(event)

    async def build_turn_context(agent, text, history):
        return Ctx(_messages())

    async def run_conversation(agent, ctx, *, event_sink=None):
        await emit_event(event_sink, "llm_start", "请求模型")
        await emit_event(event_sink, "assistant_message", "echo:hello")
        return {
            "final_response": "echo:hello",
            "messages": ctx.messages,
            "completed": True,
        }

    monkeypatch.setattr("personal_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("personal_agent.agent.loop.run_conversation", run_conversation)

    result = await svc.run_turn_events("cli:default:local", _source(), "hello", event_sink=Sink())

    assert [event.type for event in result.events] == ["llm_start", "assistant_message", "turn_end"]
    assert [event.type for event in forwarded] == ["llm_start", "assistant_message", "turn_end"]
    assert result.final_response == "echo:hello"


@pytest.mark.asyncio
async def test_run_turn_persists_minimal_context_overflow_turn(service, monkeypatch):
    svc, _manager, _db = service

    async def build_turn_context(agent, text, history):
        return Ctx(_messages())

    async def run_conversation(agent, ctx):
        return {
            "final_response": "partial",
            "messages": ctx.messages,
            "completed": False,
            "context_overflow": True,
        }

    monkeypatch.setattr("personal_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("personal_agent.agent.loop.run_conversation", run_conversation)

    result = await svc.run_turn("cli:default:local", _source(), "hello")

    session = await svc.session_store.get_or_create("cli:default:local", _source())
    history = await svc.session_store.load_history(session.session_id)
    assert result.status == "context_overflow"
    assert [msg["content"][0]["text"] for msg in history] == ["hello", "partial"]


@pytest.mark.asyncio
async def test_run_turn_persists_minimal_failed_turn(service, monkeypatch):
    svc, _manager, _db = service

    async def build_turn_context(agent, text, history):
        return Ctx(_messages(user_text=text))

    async def run_conversation(agent, ctx):
        return {
            "final_response": "抱歉，模型调用出错了：boom",
            "messages": ctx.messages,
            "completed": False,
            "status": "failed",
            "error": "RuntimeError: boom",
        }

    monkeypatch.setattr("personal_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("personal_agent.agent.loop.run_conversation", run_conversation)

    result = await svc.run_turn("cli:default:local", _source(), "hello")

    session = await svc.session_store.get_or_create("cli:default:local", _source())
    history = await svc.session_store.load_history(session.session_id)
    assert result.status == "failed"
    assert result.error == "RuntimeError: boom"
    assert [msg["content"][0]["text"] for msg in history] == ["hello", "抱歉，模型调用出错了：boom"]


@pytest.mark.asyncio
async def test_run_turn_persists_minimal_stopped_turn(service, monkeypatch):
    svc, _manager, _db = service

    async def build_turn_context(agent, text, history):
        return Ctx(_messages(user_text=text))

    async def run_conversation(agent, ctx):
        return {
            "final_response": "已停止。",
            "messages": ctx.messages,
            "completed": False,
            "status": "stopped",
        }

    monkeypatch.setattr("personal_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("personal_agent.agent.loop.run_conversation", run_conversation)

    result = await svc.run_turn("cli:default:local", _source(), "hello")

    session = await svc.session_store.get_or_create("cli:default:local", _source())
    history = await svc.session_store.load_history(session.session_id)
    assert result.status == "stopped"
    assert [msg["content"][0]["text"] for msg in history] == ["hello", "已停止。"]


@pytest.mark.asyncio
async def test_run_turn_converts_unhandled_exception_to_failed_turn(service, monkeypatch):
    svc, _manager, _db = service

    async def build_turn_context(agent, text, history):
        raise RuntimeError("context boom")

    monkeypatch.setattr("personal_agent.agent.context.build_turn_context", build_turn_context)

    result = await svc.run_turn("cli:default:local", _source(), "hello")

    session = await svc.session_store.get_or_create("cli:default:local", _source())
    history = await svc.session_store.load_history(session.session_id)
    assert result.status == "failed"
    assert result.error == "RuntimeError: context boom"
    assert [msg["content"][0]["text"] for msg in history] == ["hello", "抱歉，本轮处理出错了：context boom"]


@pytest.mark.asyncio
async def test_run_turn_creates_compressed_session_when_context_was_compressed(service, monkeypatch):
    svc, _manager, _db = service
    calls = []

    async def build_turn_context(agent, text, history):
        return Ctx(_messages(), was_compressed=True)

    async def run_conversation(agent, ctx):
        return {
            "final_response": "echo:hello",
            "messages": ctx.messages,
            "completed": True,
        }

    async def create_compressed_session(session_key, source, messages):
        calls.append((session_key, source, messages))
        return "compressed-id"

    monkeypatch.setattr("personal_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("personal_agent.agent.loop.run_conversation", run_conversation)
    monkeypatch.setattr(svc.session_store, "create_compressed_session", create_compressed_session)

    result = await svc.run_turn("cli:default:local", _source(), "hello")

    assert result.was_compressed is True
    assert calls and calls[0][0] == "cli:default:local"
    assert calls[0][2] == _messages()


@pytest.mark.asyncio
async def test_agent_cache_reuses_and_refreshes_stale_agent(service, monkeypatch):
    svc, _manager, _db = service
    created = []

    async def create_agent_runtime(*args, **kwargs):
        agent = Agent()
        created.append(agent)
        return SimpleNamespace(agent=agent)

    monkeypatch.setattr("personal_agent.agent.factory.create_agent_runtime", create_agent_runtime)

    first = await svc.get_or_create_agent("cli:new:local")
    second = await svc.get_or_create_agent("cli:new:local")
    first._tools_generation = -1
    third = await svc.get_or_create_agent("cli:new:local")

    assert first is second
    assert third is not first
    assert created == [first, third]


def test_agent_cache_operations_and_stop(service, monkeypatch):
    svc, _manager, _db = service
    monkeypatch.setattr(
        "personal_agent.plugins.builtin.tools.builtin.delegate.stop_delegate_agents",
        lambda: 0,
    )
    svc.agent_cache.clear()
    svc.agent_cache["a"] = Agent()
    svc.agent_cache["b"] = Agent()

    assert svc.get_cached_agent("a") is svc.agent_cache["a"]
    assert svc.has_cached_agent("a") is True
    svc.rename_cached_agent("a", "c")
    svc.invalidate_agent("b")
    stopped = svc.request_stop()

    assert "a" not in svc.agent_cache
    assert "b" not in svc.agent_cache
    assert "c" in svc.agent_cache
    assert svc.agent_cache["c"]._interrupt_requested is True
    assert isinstance(stopped, int)


def test_agent_cache_compat_methods_delegate_to_new_api(service, monkeypatch):
    svc, _manager, _db = service
    monkeypatch.setattr(
        "personal_agent.plugins.builtin.tools.builtin.delegate.stop_delegate_agents",
        lambda: 0,
    )
    svc.agent_cache.clear()
    svc.agent_cache["a"] = Agent()
    svc.agent_cache["b"] = Agent()

    svc.move_agent("a", "c")
    svc.delete_agent("b")
    stopped = svc.stop_all_agents()

    assert "a" not in svc.agent_cache
    assert "b" not in svc.agent_cache
    assert "c" in svc.agent_cache
    assert svc.agent_cache["c"]._interrupt_requested is True
    assert isinstance(stopped, int)


def test_agent_cache_allow_category_helpers(service):
    svc, _manager, _db = service
    svc.agent_cache.clear()
    svc.agent_cache["a"] = Agent()
    svc.agent_cache["b"] = Agent()

    assert svc.allow_agent_category("a", "write") is True
    assert svc.allow_agent_category("missing", "write") is False
    count = svc.allow_all_cached_agents("bash")

    assert count == 2
    assert svc.agent_cache["a"]._destructive_allowed == {"write", "bash"}
    assert svc.agent_cache["b"]._destructive_allowed == {"bash"}


def test_request_stop_can_target_one_cached_agent(service, monkeypatch):
    svc, _manager, _db = service
    monkeypatch.setattr(
        "personal_agent.plugins.builtin.tools.builtin.delegate.stop_delegate_agents",
        lambda: 0,
    )
    svc.agent_cache.clear()
    svc.agent_cache["a"] = Agent()
    svc.agent_cache["b"] = Agent()

    svc.request_stop("a")

    assert svc.agent_cache["a"]._interrupt_requested is True
    assert svc.agent_cache["b"]._interrupt_requested is False


@pytest.mark.asyncio
async def test_session_summary_helpers(service):
    svc, _manager, _db = service
    await svc.ensure_session("cli:default:local", _source())
    session = await svc.session_store.get_or_create("cli:default:local", _source())
    await svc.session_store.save_transcript(session.session_id, _messages())

    listed = await svc.session_list_summary(
        platform="cli",
        user_id="local",
        current_key="cli:default:local",
    )
    current = await svc.current_session_summary("cli:default:local", _source())

    assert "当前会话: cli:default:local" in listed
    assert "cli:default:local <- (2 条消息)" in listed
    assert "session id" in current
    assert "消息数: 2" in current


@pytest.mark.asyncio
async def test_session_rename_delete_update_agent_cache(service):
    svc, _manager, _db = service
    await svc.ensure_session("cli:default:local", _source())
    agent = svc.agent_cache["cli:default:local"]

    renamed = await svc.rename_session("cli:default:local", "cli:renamed:local")
    deleted = await svc.delete_session("cli:renamed:local")

    assert renamed is True
    assert deleted is True
    assert svc.session_store.get("cli:default:local") is None
    assert svc.session_store.get("cli:renamed:local") is None
    assert "cli:default:local" not in svc.agent_cache
    assert "cli:renamed:local" not in svc.agent_cache
    assert agent not in svc.agent_cache.values()


@pytest.mark.asyncio
async def test_usage_summary_reuses_cached_agent_without_forcing_creation(service):
    svc, _manager, _db = service

    empty = await svc.usage_summary(
        "cli:missing:local",
        _source(),
        create_agent=False,
        empty_message="empty",
    )
    usage = await svc.usage_summary("cli:default:local", _source())

    assert empty == "empty"
    assert "cli:missing:local" not in svc.agent_cache
    assert "会话用量" in usage
    assert "上下文窗口" in usage
