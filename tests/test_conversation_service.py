"""Shared conversation service behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from luna_agent.config import Settings
from luna_agent.compression import CompactionMetadata, CompactionResult
from luna_agent.conversation import ConversationService
from luna_agent.conversation.service import TURN_REPORT_HISTORY_LIMIT
from luna_agent.db.database import Database
from luna_agent.gateway.compression_chain import CompressionChain
from luna_agent.gateway.session_store import SessionStore
from luna_agent.models.messages import SessionSource
from luna_agent.models.messages import NormalizedResponse
from luna_agent.tools.registry import tool_registry


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


class Ctx:
    def __init__(
        self,
        messages,
        *,
        was_compressed=False,
        should_review_memory=False,
        current_turn_user_idx=0,
        compaction_result=None,
    ):
        self.messages = messages
        self.was_compressed = was_compressed
        self.should_review_memory = should_review_memory
        self.current_turn_user_idx = current_turn_user_idx
        self.compaction_result = compaction_result


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
async def test_turn_policy_snapshot_keeps_running_agent_mode_until_next_turn(service):
    svc, _manager, _db = service
    session_key = "cli:default:local"
    agent = await svc.get_or_create_agent(session_key)
    snapshot = svc.capture_turn_policy(session_key)
    snapshot.security.state.grant_tool("core:test", ttl_seconds=60)

    state = svc.set_security_mode(session_key, "local-auto")

    assert snapshot.revision == 0
    assert snapshot.security.mode_id == "ask-first"
    assert snapshot.security.state is state
    assert snapshot.security.state.tool_grants == {}
    assert agent._security_context.mode_id == "ask-first"

    same_agent = await svc.get_or_create_agent(session_key)
    assert same_agent is agent
    assert same_agent._security_context.mode_id == "local-auto"
    assert svc.capture_turn_policy(session_key).revision == 1


@pytest.mark.asyncio
async def test_conversation_hooks_emit_session_start_once_and_prompt_context(service):
    from luna_agent.conversation.input import ConversationInput
    from luna_agent.hooks import ContextHookOutcome, HookEvent, HookManager

    svc, _manager, _db = service
    hook_manager = HookManager()
    svc.hook_manager = hook_manager
    starts = []

    async def session_start(event):
        starts.append(event.payload["source"])
        return ContextHookOutcome(additional_context="session policy")

    async def prompt(event):
        return ContextHookOutcome(additional_context=f"prompt:{event.payload['text']}")

    hook_manager.register(owner="test", event=HookEvent.SESSION_START, callback=session_start)
    hook_manager.register(owner="test", event=HookEvent.USER_PROMPT_SUBMIT, callback=prompt)
    user_input = ConversationInput(text="hello", source=_source())
    session = await svc.session_store.get_or_create("cli:default:local", _source())

    first = await svc._conversation_hook_contexts(
        session_key="cli:default:local",
        session_id=session.session_id,
        turn_id="turn-1",
        source=_source(),
        user_input=user_input,
        previous_session_id=None,
    )
    second = await svc._conversation_hook_contexts(
        session_key="cli:default:local",
        session_id=session.session_id,
        turn_id="turn-2",
        source=_source(),
        user_input=user_input,
        previous_session_id=session.session_id,
    )

    assert starts == ["new"]
    assert "session policy" in first[0]
    assert any("prompt:hello" in item for item in first)
    assert second == ["[UserPromptSubmit hook context]\nprompt:hello"]


@pytest.mark.asyncio
async def test_prompt_hook_can_stop_before_agent_loop(service, monkeypatch):
    from luna_agent.hooks import ContextHookOutcome, HookEvent, HookManager

    svc, _manager, _db = service
    hook_manager = HookManager()
    svc.hook_manager = hook_manager
    called = False

    async def stop_prompt(event):
        return ContextHookOutcome(stop=True, reason="prompt rejected by policy")

    async def run_conversation(agent, ctx):
        nonlocal called
        called = True
        return {}

    hook_manager.register(
        owner="test",
        event=HookEvent.USER_PROMPT_SUBMIT,
        callback=stop_prompt,
    )
    monkeypatch.setattr("luna_agent.agent.loop.run_conversation", run_conversation)

    result = await svc.run_turn("cli:default:local", _source(), "blocked input")

    assert result.status == "stopped"
    assert result.final_response == "prompt rejected by policy"
    assert any(
        event.type == "stop" and event.data.get("reason") == "hook"
        for event in result.events
    )
    assert called is False


def _turn_report(
    *,
    status="completed",
    duration=1.25,
    error="",
    llm_calls=1,
    tool_calls=2,
    results_total=None,
    llm_tool_call_count=None,
    tool_names=None,
    status_counts=None,
    user_message_summary="hello",
    final_response_summary="echo:hello",
    claimed_but_no_tool_call=False,
    tool_truth_warnings=None,
    input_tokens=10,
    output_tokens=5,
    retries=None,
):
    return {
        "status": status,
        "duration": duration,
        "error": error,
        "user_message_summary": user_message_summary,
        "final_response_summary": final_response_summary,
        "llm": {
            "calls": llm_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
        "tools": {"total": tool_calls, "items": []},
        "tool_truth": {
            "calls_total": tool_calls,
            "results_total": tool_calls if results_total is None else results_total,
            "llm_tool_call_count": tool_calls if llm_tool_call_count is None else llm_tool_call_count,
            "tool_names": list(tool_names or []),
            "status_counts": dict(status_counts or {"success": tool_calls}),
            "warnings": list(tool_truth_warnings or []),
            "assistant_claim": {
                "claimed_tool_use": bool(claimed_but_no_tool_call),
                "claim_phrases": [],
                "claimed_but_no_tool_call": claimed_but_no_tool_call,
            },
        },
        "retries": list(retries or []),
    }


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

    monkeypatch.setattr("luna_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("luna_agent.agent.loop.run_conversation", run_conversation)

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
    from luna_agent.conversation.events import emit_event

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
            "turn_report": _turn_report(llm_calls=1, tool_calls=0),
        }

    monkeypatch.setattr("luna_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("luna_agent.agent.loop.run_conversation", run_conversation)

    result = await svc.run_turn_events("cli:default:local", _source(), "hello", event_sink=Sink())

    assert [event.type for event in result.events] == ["llm_start", "assistant_message", "turn_end"]
    assert [event.type for event in forwarded] == ["llm_start", "assistant_message", "turn_end"]
    assert result.final_response == "echo:hello"
    assert result.turn_report["status"] == "completed"
    assert result.turn_report["llm"]["calls"] == 1
    assert len(svc.turn_reports) == 1
    envelope = svc.turn_reports[-1]
    assert envelope["session_key"] == "cli:default:local"
    assert envelope["source"]["platform"] == "cli"
    assert envelope["source"]["user_id"] == "local"
    assert envelope["status"] == "completed"
    assert envelope["report"] == result.turn_report
    summary = svc.turn_report_summary()
    assert summary["stored"] == 1
    assert summary["last_status"] == "completed"
    assert summary["last_duration"] == 1.25
    assert summary["last_llm_calls"] == 1
    assert summary["last_tool_calls"] == 0
    assert summary["last_claimed_but_no_tool_call"] is False
    assert summary["last_tool_truth_warnings"] == []
    assert summary["last_input_tokens"] == 10
    assert summary["last_output_tokens"] == 5
    assert summary["last_retries"] == 0
    persisted = await svc.recent_persisted_turn_reports(limit=5)
    assert len(persisted) == 1
    assert persisted[0]["session_key"] == "cli:default:local"
    assert persisted[0]["status"] == "completed"
    assert persisted[0]["report"] == result.turn_report
    persisted_summary = await svc.persisted_turn_report_summary()
    assert persisted_summary["stored"] == 1
    assert persisted_summary["last_status"] == "completed"
    assert persisted_summary["last_session_key"] == "cli:default:local"


@pytest.mark.asyncio
async def test_run_turn_events_forwards_steer_manager_and_records_summary(service, monkeypatch):
    svc, _manager, _db = service
    seen = {}

    async def build_turn_context(agent, text, history, *, turn_id=None):
        assert turn_id
        ctx = Ctx(_messages())
        ctx.turn_id = turn_id
        return ctx

    async def run_conversation(agent, ctx, *, steer=None, session_key=""):
        signal = steer.add(session_key, None, "补充说明")
        consumed = steer.consume(session_key, ctx.turn_id)
        seen["signal_id"] = signal.id
        seen["consumed"] = [item.id for item in consumed]
        return {
            "final_response": "echo:hello",
            "messages": ctx.messages,
            "completed": True,
            "turn_report": _turn_report(llm_calls=1, tool_calls=0),
        }

    monkeypatch.setattr("luna_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("luna_agent.agent.loop.run_conversation", run_conversation)

    result = await svc.run_turn_events("cli:default:local", _source(), "hello")

    assert seen["consumed"] == [seen["signal_id"]]
    assert result.turn_report["steer"]["received"] == 1
    assert result.turn_report["steer"]["consumed"] == 1
    assert result.turn_report["steer"]["items"][0]["id"] == seen["signal_id"]


@pytest.mark.asyncio
async def test_run_turn_events_forwards_confirm_callback(service, monkeypatch):
    svc, _manager, _db = service
    confirm_seen = []

    async def confirm(decision):
        return "allow"

    async def build_turn_context(agent, text, history):
        return Ctx(_messages())

    async def run_conversation(agent, ctx, *, event_sink=None, confirm=None):
        confirm_seen.append(confirm)
        return {
            "final_response": "echo:hello",
            "messages": ctx.messages,
            "completed": True,
            "turn_report": _turn_report(llm_calls=1, tool_calls=0),
        }

    monkeypatch.setattr("luna_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("luna_agent.agent.loop.run_conversation", run_conversation)

    result = await svc.run_turn_events(
        "cli:default:local",
        _source(),
        "hello",
        confirm=confirm,
    )

    assert result.final_response == "echo:hello"
    assert confirm_seen == [confirm]


@pytest.mark.asyncio
async def test_run_turn_events_keeps_legacy_loop_without_confirm(service, monkeypatch):
    svc, _manager, _db = service
    called = []

    async def confirm(decision):
        return "allow"

    async def build_turn_context(agent, text, history):
        return Ctx(_messages())

    async def run_conversation(agent, ctx):
        called.append(True)
        return {
            "final_response": "echo:hello",
            "messages": ctx.messages,
            "completed": True,
        }

    monkeypatch.setattr("luna_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("luna_agent.agent.loop.run_conversation", run_conversation)

    result = await svc.run_turn_events(
        "cli:default:local",
        _source(),
        "hello",
        confirm=confirm,
    )

    assert result.final_response == "echo:hello"
    assert called == [True]


@pytest.mark.asyncio
async def test_run_turn_persists_tool_runs_from_events(service, monkeypatch):
    from luna_agent.conversation.events import emit_event

    svc, _manager, _db = service

    async def build_turn_context(agent, text, history):
        return Ctx(_messages(user_text=text))

    async def run_conversation(agent, ctx, *, event_sink=None):
        await emit_event(
            event_sink,
            "turn_start",
            "开始处理",
            turn_id="turn-tool",
            user_message="run pwd",
        )
        await emit_event(
            event_sink,
            "tool_start",
            "调用工具 bash",
            tool_name="bash",
            tool_use_id="call-1",
            input_summary='{"cmd": "pwd"}',
        )
        await emit_event(
            event_sink,
            "tool_end",
            "工具 bash success",
            tool_name="bash",
            tool_use_id="call-1",
            status="success",
            category="",
            duration=0.25,
            input_summary='{"cmd": "pwd"}',
            output_summary="/workspace",
            full_output="/workspace",
            output_truncated=False,
            guard_stage="runtime_guard",
            guard_reason_code="allowed",
            permission_category="bash",
            permission_decision="allow",
            required_allow="",
            execution_mode="full-auto",
            grant_matched="",
        )
        await emit_event(
            event_sink,
            "tool_end",
            "工具 tool_call success",
            tool_name="tool_call",
            tool_use_id="wrapper-1",
            status="success",
            count_as_tool=False,
        )
        return {
            "final_response": "done",
            "messages": _messages(user_text="run pwd", assistant_text="done"),
            "completed": True,
            "turn_report": _turn_report(
                tool_calls=1,
                tool_names=["bash"],
                status_counts={"success": 1},
            ) | {"turn_id": "turn-tool"},
        }

    monkeypatch.setattr("luna_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("luna_agent.agent.loop.run_conversation", run_conversation)

    result = await svc.run_turn("cli:default:local", _source(), "run pwd")
    runs = await svc.recent_tool_runs(limit=5)
    summary = svc.tool_run_memory_summary()
    db_summary = await svc.tool_run_summary(limit=5)

    assert result.status == "completed"
    assert len(runs) == 1
    run = runs[0]
    assert run["session_key"] == "cli:default:local"
    assert run["turn_id"] == "turn-tool"
    assert run["tool_use_id"] == "call-1"
    assert run["tool_name"] == "bash"
    assert run["status"] == "success"
    assert run["full_output"] == "/workspace"
    assert run["guard_stage"] == "runtime_guard"
    assert run["reason_code"] == "allowed"
    assert summary["inspected"] == 1
    assert summary["tool_counts"] == {"bash": 1}
    assert summary["status_counts"] == {"success": 1}
    assert db_summary["inspected"] == 1
    assert db_summary["tool_counts"] == {"bash": 1}
    reports = await svc.recent_persisted_turn_reports(
        limit=5,
        session_key="cli:default:local",
    )
    assert len(reports) == 1
    assert reports[0]["turn_id"] == "turn-tool"
    report_runs = await svc.tool_runs_for_turn_report(reports[0]["id"])
    assert len(report_runs) == 1
    assert report_runs[0]["tool_use_id"] == "call-1"


@pytest.mark.asyncio
async def test_query_service_exposes_tool_runs_and_summaries(service, monkeypatch):
    from luna_agent.conversation.events import emit_event

    svc, _manager, _db = service

    async def build_turn_context(agent, text, history):
        return Ctx(_messages(user_text=text))

    async def run_conversation(agent, ctx, *, event_sink=None):
        await emit_event(event_sink, "turn_start", "开始处理", turn_id="turn-query")
        await emit_event(
            event_sink,
            "tool_end",
            "工具 read success",
            tool_name="read",
            tool_use_id="call-read",
            status="success",
            category="read",
            duration=0.1,
            input_summary='{"path": "README.md"}',
            output_summary="README",
            full_output="README contents",
            output_truncated=False,
            permission_category="read",
            permission_decision="allow",
            execution_mode="standard",
        )
        await emit_event(
            event_sink,
            "tool_end",
            "工具 bash denied",
            tool_name="bash",
            tool_use_id="call-bash",
            status="denied",
            category="authorization",
            duration=0.0,
            input_summary='{"cmd": "rm -rf /"}',
            output_summary="",
            error="blocked",
            permission_category="bash",
            permission_decision="deny",
            execution_mode="standard",
        )
        return {
            "final_response": "done",
            "messages": _messages(user_text="inspect", assistant_text="done"),
            "completed": True,
        }

    monkeypatch.setattr("luna_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("luna_agent.agent.loop.run_conversation", run_conversation)

    await svc.run_turn("cli:default:local", _source(), "inspect")

    recent = await svc.queries.recent_tool_runs(limit=10, session_key="cli:default:local")
    summary = await svc.queries.tool_run_summary(limit=10, session_key="cli:default:local")
    detail = await svc.queries.tool_run_detail(recent["items"][0]["id"])

    assert recent["scope"] == "session"
    assert recent["session_key"] == "cli:default:local"
    assert [item["tool_name"] for item in recent["items"]] == ["read", "bash"]
    assert summary["inspected"] == 2
    assert summary["tool_counts"] == {"bash": 1, "read": 1}
    assert summary["status_counts"] == {"denied": 1, "success": 1}
    assert summary["denied"] == 1
    assert detail is not None
    assert detail["tool_name"] == "read"
    assert detail["full_output"] == "README contents"
    assert await svc.queries.tool_run_detail(999999) is None


@pytest.mark.asyncio
async def test_run_turn_keeps_empty_turn_report_for_legacy_loop(service, monkeypatch):
    svc, _manager, _db = service

    async def build_turn_context(agent, text, history):
        return Ctx(_messages())

    async def run_conversation(agent, ctx):
        return {
            "final_response": "echo:hello",
            "messages": ctx.messages,
            "completed": True,
        }

    monkeypatch.setattr("luna_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("luna_agent.agent.loop.run_conversation", run_conversation)

    result = await svc.run_turn("cli:default:local", _source(), "hello")

    assert result.turn_report == {}
    assert len(svc.turn_reports) == 0
    assert svc.turn_report_summary()["stored"] == 0
    assert await svc.recent_persisted_turn_reports(limit=5) == []


@pytest.mark.asyncio
async def test_run_turn_records_failed_stopped_and_context_reports(service, monkeypatch):
    svc, _manager, _db = service
    reports = [
        _turn_report(status="failed", error="RuntimeError: boom"),
        _turn_report(status="stopped", duration=0.5, llm_calls=0, tool_calls=0),
        _turn_report(status="context_overflow", duration=0.1, retries=[{"category": "limit"}]),
    ]

    async def build_turn_context(agent, text, history):
        return Ctx(_messages(user_text=text))

    async def run_conversation(agent, ctx):
        report = reports.pop(0)
        return {
            "final_response": "result",
            "messages": ctx.messages,
            "completed": report["status"] == "completed",
            "status": report["status"],
            "error": report["error"],
            "context_overflow": report["status"] == "context_overflow",
            "turn_report": report,
        }

    monkeypatch.setattr("luna_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("luna_agent.agent.loop.run_conversation", run_conversation)

    failed = await svc.run_turn("cli:default:local", _source(), "failed")
    stopped = await svc.run_turn("cli:default:local", _source(), "stopped")
    overflow = await svc.run_turn("cli:default:local", _source(), "overflow")

    assert failed.status == "failed"
    assert stopped.status == "stopped"
    assert overflow.status == "context_overflow"
    assert [item["status"] for item in svc.turn_reports] == ["failed", "stopped", "context_overflow"]
    persisted_failed = await svc.recent_persisted_turn_reports(limit=5, status="failed")
    persisted_stopped = await svc.recent_persisted_turn_reports(limit=5, status="stopped")
    persisted_overflow = await svc.recent_persisted_turn_reports(limit=5, status="context_overflow")
    assert [item["status"] for item in persisted_failed] == ["failed"]
    assert [item["status"] for item in persisted_stopped] == ["stopped"]
    assert [item["status"] for item in persisted_overflow] == ["context_overflow"]
    persisted_summary = svc.turn_report_persistence_summary()
    assert persisted_summary["stored"] == 3
    assert persisted_summary["last_status"] == "context_overflow"
    summary = svc.turn_report_summary()
    assert summary["stored"] == 3
    assert summary["last_status"] == "context_overflow"
    assert summary["last_retries"] == 1


@pytest.mark.asyncio
async def test_turn_report_history_keeps_recent_limit(service):
    svc, _manager, _db = service
    source = _source()

    for index in range(TURN_REPORT_HISTORY_LIMIT + 5):
        svc.record_turn_report(
            f"cli:{index}:local",
            source,
            _turn_report(status="completed", duration=float(index), llm_calls=index),
        )

    assert len(svc.turn_reports) == TURN_REPORT_HISTORY_LIMIT
    assert svc.turn_reports[0]["session_key"] == "cli:5:local"
    assert svc.turn_reports[-1]["session_key"] == f"cli:{TURN_REPORT_HISTORY_LIMIT + 4}:local"
    recent = svc.recent_turn_reports(limit=3)
    assert [item["session_key"] for item in recent] == [
        f"cli:{TURN_REPORT_HISTORY_LIMIT + 2}:local",
        f"cli:{TURN_REPORT_HISTORY_LIMIT + 3}:local",
        f"cli:{TURN_REPORT_HISTORY_LIMIT + 4}:local",
    ]


@pytest.mark.asyncio
async def test_turn_report_summary_includes_tool_truth_warnings(service):
    svc, _manager, _db = service
    svc.record_turn_report(
        "cli:default:local",
        _source(),
        _turn_report(
            tool_calls=0,
            claimed_but_no_tool_call=True,
            tool_truth_warnings=["assistant_claimed_tool_use_without_tool_call"],
        ),
    )

    summary = svc.turn_report_summary()

    assert summary["last_tool_calls"] == 0
    assert summary["last_claimed_but_no_tool_call"] is True
    assert summary["last_tool_truth_warnings"] == [
        "assistant_claimed_tool_use_without_tool_call"
    ]


@pytest.mark.asyncio
async def test_recent_tool_truth_returns_stable_snapshots(service):
    svc, _manager, _db = service
    svc.record_turn_report(
        "cli:default:local",
        _source(),
        _turn_report(
            tool_calls=2,
            results_total=1,
            llm_tool_call_count=2,
            tool_names=["bash", "search"],
            status_counts={"success": 1, "denied": 1},
            user_message_summary="run ls",
            final_response_summary="done",
        ),
    )

    recent = svc.recent_tool_truth()

    assert len(recent) == 1
    item = recent[0]
    assert item["session_key"] == "cli:default:local"
    assert item["source"]["platform"] == "cli"
    assert item["status"] == "completed"
    assert item["user_message_summary"] == "run ls"
    assert item["final_response_summary"] == "done"
    assert item["calls_total"] == 2
    assert item["results_total"] == 1
    assert item["llm_tool_call_count"] == 2
    assert item["tool_names"] == ["bash", "search"]
    assert item["status_counts"]["success"] == 1
    assert item["status_counts"]["denied"] == 1
    assert item["status_counts"]["error"] == 0
    assert item["claimed_but_no_tool_call"] is False


@pytest.mark.asyncio
async def test_tool_truth_summary_aggregates_recent_reports(service):
    svc, _manager, _db = service
    source = _source()
    svc.record_turn_report(
        "cli:tools:local",
        source,
        _turn_report(
            tool_calls=2,
            tool_names=["bash", "bash"],
            status_counts={"success": 1, "error": 1},
        ),
    )
    svc.record_turn_report(
        "cli:claim:local",
        source,
        _turn_report(
            tool_calls=0,
            tool_names=[],
            status_counts={},
            claimed_but_no_tool_call=True,
            tool_truth_warnings=["assistant_claimed_tool_use_without_tool_call"],
        ),
    )
    svc.record_turn_report(
        "cli:denied:local",
        source,
        _turn_report(
            tool_calls=1,
            tool_names=["write_file"],
            status_counts={"denied": 1},
        ),
    )

    summary = svc.tool_truth_summary()

    assert summary["stored"] == 3
    assert summary["inspected"] == 3
    assert summary["turns_with_tools"] == 2
    assert summary["turns_without_tools"] == 1
    assert summary["claim_mismatches"] == 1
    assert summary["tool_counts"] == {"bash": 2, "write_file": 1}
    assert summary["status_counts"]["success"] == 1
    assert summary["status_counts"]["error"] == 1
    assert summary["status_counts"]["denied"] == 1
    assert summary["denied_tool_calls"] == 1
    assert summary["failed_tool_calls"] == 1
    assert summary["warning_counts"] == {
        "assistant_claimed_tool_use_without_tool_call": 1
    }
    assert summary["last_warning"] == ""
    assert summary["last_claimed_but_no_tool_call"] is False


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

    monkeypatch.setattr("luna_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("luna_agent.agent.loop.run_conversation", run_conversation)

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

    monkeypatch.setattr("luna_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("luna_agent.agent.loop.run_conversation", run_conversation)

    result = await svc.run_turn("cli:default:local", _source(), "hello")

    session = await svc.session_store.get_or_create("cli:default:local", _source())
    history = await svc.session_store.load_history(session.session_id)
    assert result.status == "failed"
    assert result.error == "RuntimeError: boom"
    assert [msg["content"][0]["text"] for msg in history] == ["hello", "抱歉，模型调用出错了：boom"]


@pytest.mark.asyncio
async def test_run_turn_persists_completed_assistant_text_before_stop(service, monkeypatch):
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

    monkeypatch.setattr("luna_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("luna_agent.agent.loop.run_conversation", run_conversation)

    result = await svc.run_turn("cli:default:local", _source(), "hello")

    session = await svc.session_store.get_or_create("cli:default:local", _source())
    history = await svc.session_store.load_history(session.session_id)
    assert result.status == "stopped"
    assert [msg["content"][0]["text"] for msg in history] == ["hello", "echo:hello", "已停止。"]


@pytest.mark.asyncio
async def test_run_turn_persists_completed_tools_before_stop(service, monkeypatch):
    svc, _manager, _db = service
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "write it"}]},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "call-1", "name": "write_file", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "written"}],
        },
    ]

    async def build_turn_context(agent, text, history):
        return Ctx(messages)

    async def run_conversation(agent, ctx):
        return {
            "final_response": "已停止。",
            "messages": ctx.messages,
            "completed": False,
            "status": "stopped",
            "turn_report": _turn_report(status="stopped", llm_calls=1, tool_calls=1),
        }

    monkeypatch.setattr("luna_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("luna_agent.agent.loop.run_conversation", run_conversation)

    result = await svc.run_turn("cli:default:local", _source(), "write it")

    session = await svc.session_store.get_or_create("cli:default:local", _source())
    history = await svc.session_store.load_history(session.session_id)
    assert [msg["content"][0]["text"] for msg in history] == ["write it", "written", "已停止。"]
    assert result.turn_report["persistence"]["partial"] is True
    assert result.turn_report["persistence"]["tool_calls_saved"] == 1


@pytest.mark.asyncio
async def test_run_turn_converts_unhandled_exception_to_failed_turn(service, monkeypatch):
    svc, _manager, _db = service

    async def build_turn_context(agent, text, history):
        raise RuntimeError("context boom")

    monkeypatch.setattr("luna_agent.agent.context.build_turn_context", build_turn_context)

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

    monkeypatch.setattr("luna_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("luna_agent.agent.loop.run_conversation", run_conversation)
    monkeypatch.setattr(svc.session_store, "create_compressed_session", create_compressed_session)

    result = await svc.run_turn("cli:default:local", _source(), "hello")

    assert result.was_compressed is True
    assert calls and calls[0][0] == "cli:default:local"
    assert calls[0][2] == _messages()


@pytest.mark.asyncio
async def test_compaction_checkpoint_is_persisted_before_failed_turn(service, monkeypatch):
    svc, _manager, _db = service
    source = _source()
    original = await svc.session_store.get_or_create("cli:default:local", source)
    await svc.session_store.save_transcript(original.session_id, _messages("old", "answer"), 0)
    replacement = [{
        "role": "user",
        "content": [{"type": "text", "text": "[Context checkpoint summary]\nstate"}],
    }]
    checkpoint = CompactionResult(
        replacement_history=replacement,
        summary="state",
        metadata=CompactionMetadata(pre_tokens=900, post_tokens=100),
    )

    async def build_turn_context(agent, text, history):
        return Ctx(
            replacement + [{"role": "user", "content": [{"type": "text", "text": text}]}],
            was_compressed=True,
            current_turn_user_idx=1,
            compaction_result=checkpoint,
        )

    async def run_conversation(agent, ctx):
        raise RuntimeError("model down after checkpoint")

    monkeypatch.setattr("luna_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("luna_agent.agent.loop.run_conversation", run_conversation)

    result = await svc.run_turn("cli:default:local", source, "current request")

    current_id = svc.session_store.resolve_session_id("cli:default:local")
    assert current_id != original.session_id
    assert svc.compression_chain.get_chain(original.session_id) == [original.session_id, current_id]
    assert result.status == "failed"
    old_history = await svc.session_store.load_history(original.session_id)
    new_history = await svc.session_store.load_history(current_id)
    assert any("old" in str(message) for message in old_history)
    assert any("state" in str(message) for message in new_history)
    assert any("current request" in str(message) for message in new_history)
    assert sum("current request" in str(message) for message in new_history) == 1


@pytest.mark.asyncio
async def test_mid_turn_checkpoint_sink_rebases_final_transcript(service, monkeypatch):
    svc, _manager, _db = service
    source = _source()
    original = await svc.session_store.get_or_create("cli:default:local", source)
    replacement = [
        {"role": "user", "content": [{"type": "text", "text": "current request"}]},
        {"role": "user", "content": [{"type": "text", "text": "[Context checkpoint summary]\nworking"}]},
    ]
    compaction = CompactionResult(
        replacement_history=replacement,
        summary="working",
        metadata=CompactionMetadata(trigger="mid_turn", pre_tokens=900, post_tokens=100),
    )

    async def build_turn_context(agent, text, history):
        return Ctx(
            [{"role": "user", "content": [{"type": "text", "text": text}]}],
            current_turn_user_idx=0,
        )

    async def run_conversation(agent, ctx, checkpoint_sink=None):
        await checkpoint_sink(compaction)
        ctx.messages = replacement + [
            {"role": "assistant", "content": [{"type": "text", "text": "finished"}]}
        ]
        return {
            "final_response": "finished",
            "messages": ctx.messages,
            "completed": True,
        }

    monkeypatch.setattr("luna_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("luna_agent.agent.loop.run_conversation", run_conversation)

    result = await svc.run_turn("cli:default:local", source, "current request")

    current_id = svc.session_store.resolve_session_id("cli:default:local")
    history = await svc.session_store.load_history(current_id)
    assert result.status == "completed"
    assert current_id != original.session_id
    assert sum("current request" in str(message) for message in history) == 1
    assert sum("finished" in str(message) for message in history) == 1


@pytest.mark.asyncio
async def test_manual_compaction_uses_shared_checkpoint_path(service):
    from luna_agent.compression.simple import ContextCompressor

    svc, _manager, _db = service
    source = _source()
    session = await svc.session_store.get_or_create("cli:default:local", source)
    await svc.session_store.save_transcript(
        session.session_id,
        _messages("detailed requirement", "detailed answer"),
        0,
    )
    agent = await svc.get_or_create_agent("cli:default:local")
    agent._compressor = ContextCompressor(output_tokens=8192)
    agent._transport = AsyncMock()
    agent._transport.call.return_value = NormalizedResponse(
        text="complete manual handoff",
        tool_calls=[],
        usage={"input_tokens": 100, "output_tokens": 20},
        finish_reason="end_turn",
        stop_reason="end_turn",
        model="test",
    )

    checkpoint = await svc.compact_session("cli:default:local", source)

    assert checkpoint is not None
    assert checkpoint["window_number"] == 1
    assert checkpoint["metadata"].trigger == "manual"
    current_id = svc.session_store.resolve_session_id("cli:default:local")
    history = await svc.session_store.load_history(current_id)
    assert any("complete manual handoff" in str(message) for message in history)


@pytest.mark.asyncio
async def test_agent_cache_reuses_and_refreshes_stale_agent(service, monkeypatch):
    svc, _manager, _db = service
    created = []

    async def create_agent_runtime(*args, **kwargs):
        agent = Agent()
        created.append(agent)
        return SimpleNamespace(agent=agent)

    monkeypatch.setattr("luna_agent.agent.factory.create_agent_runtime", create_agent_runtime)

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
        "luna_agent.plugins.builtin.tools.builtin.delegate.stop_delegate_agents",
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
        "luna_agent.plugins.builtin.tools.builtin.delegate.stop_delegate_agents",
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


def test_request_stop_can_target_one_cached_agent(service, monkeypatch):
    svc, _manager, _db = service
    monkeypatch.setattr(
        "luna_agent.plugins.builtin.tools.builtin.delegate.stop_delegate_agents",
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
    assert "最近一轮工具执行" in usage
    assert "本轮工具调用" not in usage
