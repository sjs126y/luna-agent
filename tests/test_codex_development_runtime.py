from __future__ import annotations

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
        notify_sessions=["wechat:test"],
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
async def test_events_are_bounded_and_delivered_with_event_specific_context(runtime):
    conversation = Conversation()
    runtime._active_ctx = SimpleNamespace(resources=SimpleNamespace(conversation=conversation))
    await runtime.create("demo", "demo plugin")
    for index in range(55):
        await runtime._on_message("demo", {"method": "item/agentMessage/completed", "params": {"text": f"message {index}"}})
    events = runtime.events("demo", 20)
    assert len(events) == 20
    assert events[-1]["text"] == "message 54"
    assert conversation.intents
    assert "Codex 的新消息" in conversation.intents[-1].instruction


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
