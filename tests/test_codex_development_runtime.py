from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.codex_bridge.development import (
    CodexDevelopmentRuntime,
    _initial_development_prompt,
)


class MemoryStorage:
    def __init__(self):
        self.values = {}

    def read_json(self, name, default=None, **kwargs):
        return self.values.get(name, default)

    def write_json_atomic(self, name, value):
        self.values[name] = value


class Conversation:
    def __init__(self):
        self.intents = []

    async def submit_intent(self, intent):
        self.intents.append(intent)
        return object()


@pytest.fixture
def runtime(tmp_path):
    storage = MemoryStorage()
    config = SimpleNamespace(
        development_root=tmp_path / "workspaces",
        development_spec_path=tmp_path / "plugin-development.md",
        development_spec_revision="1",
        command="codex",
        runtime_codex_home=tmp_path / "codex-home",
        approval_policy="on-request",
        approvals_reviewer="user",
        sandbox="workspace-write",
        app_server_timeout_seconds=2.0,
        event_retention=1000,
        active=SimpleNamespace(enabled=True, sessions=["wechat:test"]),
    )
    config.development_spec_path.write_text("# plugin contract\n", encoding="utf-8")
    ctx = SimpleNamespace(storage=storage)
    return CodexDevelopmentRuntime(config=config, ctx=ctx)


@pytest.mark.asyncio
async def test_create_writes_external_scaffold_and_persists_session(runtime):
    result = await runtime.create("hello-world", "Convert a document.", "Use a single tool.")
    workspace = Path(result["workspace_path"])
    assert workspace.is_dir()
    assert (workspace / "plugin.yaml").is_file()
    assert (workspace / "PLUGIN_BRIEF.md").read_text(encoding="utf-8").count("Convert a document.") == 1
    assert (workspace / "LUNA_PLUGIN_DEVELOPMENT.md").is_file()
    assert runtime.status("hello-world")["status"] == "created"
    assert (await runtime.create("hello-world", "changed"))["status"] == "created"


@pytest.mark.asyncio
async def test_events_are_bounded_and_turn_completion_delivers_last_message(runtime):
    conversation = Conversation()
    runtime._active_ctx = SimpleNamespace(resources=SimpleNamespace(conversation=conversation))
    await runtime.create("demo", "demo plugin")
    for index in range(55):
        await runtime._on_message("demo", {"method": "item/agentMessage/completed", "params": {"text": f"message {index}"}})
    events = runtime.events("demo", limit=20)
    assert events["total"] == 55
    assert events["returned"] == 20
    assert events["events"][0]["text"] == "message 54"
    assert events["next_offset"] == 20
    second_page = runtime.events("demo", limit=20, offset=20, order="desc")
    assert second_page["events"][0]["text"] == "message 34"
    full = runtime.events("demo", limit=1, detail="full")
    assert "metadata" in full["events"][0]
    assert conversation.intents == []

    await runtime._on_message("demo", {
        "method": "turn/completed",
        "params": {"turn": {"id": "turn-1", "status": "completed"}},
    })
    assert len(conversation.intents) == 1
    assert "本轮 Codex 交流已经结束" in conversation.intents[0].instruction
    assert "message 54" in conversation.intents[0].instruction
    assert runtime.status("demo")["last_result"] == "message 54"


@pytest.mark.asyncio
async def test_event_output_exposes_local_time_and_preserves_utc(runtime):
    await runtime.create("timestamps", "demo plugin")
    session = runtime.store.get("timestamps")
    session.events.append({
        "event_id": "event-1",
        "event_type": "progress",
        "text": "working",
        "created_at": "2026-07-20T04:27:57+00:00",
        "turn_id": "turn-1",
        "metadata": {},
    })
    runtime.store.put(session)

    event = runtime.events("timestamps", limit=1)["events"][0]

    assert event["created_at_utc"] == "2026-07-20T04:27:57+00:00"
    local = datetime.fromisoformat(event["created_at"])
    assert local.utcoffset() is not None
    assert local == datetime.fromisoformat(event["created_at_utc"]).astimezone()


@pytest.mark.asyncio
async def test_progress_and_retry_events_are_persisted_without_waking_conversation(runtime):
    conversation = Conversation()
    runtime._active_ctx = SimpleNamespace(resources=SimpleNamespace(conversation=conversation))
    await runtime.create("quiet", "demo plugin")

    await runtime._on_message("quiet", {
        "method": "thread/status/changed",
        "params": {"status": "running"},
    })
    await runtime._on_message("quiet", {
        "method": "error",
        "params": {
            "message": "Reconnecting... 2/5",
            "willRetry": True,
            "turnId": "turn-retry",
        },
    })

    assert runtime.events("quiet", limit=10)["total"] == 2
    assert conversation.intents == []


@pytest.mark.asyncio
async def test_terminal_error_and_failed_completion_wake_conversation_once(runtime):
    conversation = Conversation()
    runtime._active_ctx = SimpleNamespace(resources=SimpleNamespace(conversation=conversation))
    await runtime.create("failed", "demo plugin")

    await runtime._on_message("failed", {
        "method": "error",
        "params": {
            "message": "connection failed",
            "willRetry": False,
            "turnId": "turn-failed",
        },
    })
    await runtime._on_message("failed", {
        "method": "turn/completed",
        "params": {"turn": {
            "id": "turn-failed",
            "status": "failed",
            "error": {"message": "connection failed"},
        }},
    })

    assert len(conversation.intents) == 1
    assert "Codex 开发出现错误" in conversation.intents[0].instruction


@pytest.mark.asyncio
async def test_notification_request_ids_are_unique_per_target_session(runtime):
    conversation = Conversation()
    runtime._active_ctx = SimpleNamespace(resources=SimpleNamespace(conversation=conversation))
    runtime.config.active.sessions = ["wechat:first", "wechat:second"]
    await runtime.create("targets", "demo plugin")

    await runtime._on_message("targets", {
        "id": "approval-1",
        "method": "item/commandExecution/requestApproval",
        "params": {"command": "pytest"},
    })

    assert len(conversation.intents) == 2
    assert len({intent.request_id for intent in conversation.intents}) == 2


@pytest.mark.asyncio
async def test_approval_requests_are_not_auto_approved(runtime):
    await runtime.create("approval-demo", "demo plugin")
    await runtime._on_message("approval-demo", {
        "id": "approval-1",
        "method": "item/commandExecution/requestApproval",
        "params": {"command": "pytest"},
    })
    assert runtime.approvals("approval-demo")[0]["request_id"] == "approval-1"


def test_first_turn_loads_contract_around_plain_feature_request():
    prompt = _initial_development_prompt("把 docx 转换成 Markdown")
    assert "LUNA_PLUGIN_DEVELOPMENT.md" in prompt
    assert "PLUGIN_BRIEF.md" in prompt
    assert "把 docx 转换成 Markdown" in prompt
    assert "Do not install" in prompt


@pytest.mark.asyncio
async def test_nested_codex_error_is_human_readable(runtime):
    await runtime.create("errors", "demo plugin")
    await runtime._on_message("errors", {
        "method": "error",
        "params": {"error": {
            "message": "Reconnecting... 2/5",
            "additionalDetails": "failed to lookup address information",
        }},
    })
    event = runtime.events("errors", limit=1)["events"][0]
    assert event["text"] == "Reconnecting... 2/5 - failed to lookup address information"
