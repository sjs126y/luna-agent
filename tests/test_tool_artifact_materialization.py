from __future__ import annotations

import base64

import pytest
import pytest_asyncio

from personal_agent.artifacts import ArtifactStore
from personal_agent.agent.agent import Agent
from personal_agent.db.database import Database
from personal_agent.tools.entry import ToolArtifact, ToolEntry, ToolHandlerOutput
from personal_agent.tools.executor import execute_tool_call_result, format_tool_result
from personal_agent.tools.registry import tool_registry


@pytest_asyncio.fixture
async def artifact_runtime(tmp_path):
    db = Database(tmp_path / "state.db")
    await db.initialize()
    store = ArtifactStore(tmp_path / "artifacts", db)
    await store.initialize()
    yield store
    await db.close()


@pytest.mark.asyncio
async def test_executor_materializes_artifact_and_exposes_only_reference(artifact_runtime):
    from personal_agent.conversation.events import EventRecorder

    async def handler():
        return ToolHandlerOutput(
            text="screenshot captured",
            artifacts=[ToolArtifact(
                kind="image",
                name="shot.png",
                mime_type="image/png",
                data=base64.b64encode(b"png-data").decode(),
            )],
        )

    tool_registry.register(ToolEntry(
        name="artifact_materialize_demo",
        description="artifact demo",
        schema={},
        handler=handler,
    ))
    agent = Agent(_memory_session_key="wechat:user", _hook_turn_id="turn-1")
    agent._artifact_store = artifact_runtime
    events = EventRecorder()
    try:
        result = await execute_tool_call_result(
            {"id": "call-1", "name": "artifact_materialize_demo", "input": {}},
            agent=agent,
            event_sink=events,
        )
    finally:
        tool_registry.unregister("artifact_materialize_demo")

    assert result.status == "success", result.error
    assert len(result.artifacts) == 1
    ref = result.artifacts[0]
    assert ref.artifact_id.startswith("art_")
    assert (await artifact_runtime.resolve_path(ref)).read_bytes() == b"png-data"
    visible = format_tool_result(result)
    assert ref.artifact_id in visible
    assert "png-data" not in visible
    assert "relative_path" not in visible
    available = [event for event in events.events if event.type == "artifact_available"]
    assert available[0].data["artifacts"][0]["artifact_id"] == ref.artifact_id


@pytest.mark.asyncio
async def test_executor_marks_invalid_artifact_unavailable_without_failing_tool(artifact_runtime):
    async def handler():
        return ToolHandlerOutput(
            text="render complete",
            artifacts=[ToolArtifact(kind="image", data="not-base64")],
        )

    tool_registry.register(ToolEntry(
        name="artifact_invalid_demo",
        description="invalid artifact demo",
        schema={},
        handler=handler,
    ))
    agent = Agent(_memory_session_key="wechat:user", _hook_turn_id="turn-1")
    agent._artifact_store = artifact_runtime
    try:
        result = await execute_tool_call_result(
            {"id": "call-2", "name": "artifact_invalid_demo", "input": {}},
            agent=agent,
        )
    finally:
        tool_registry.unregister("artifact_invalid_demo")

    assert result.status == "success", result.error
    assert result.artifacts == []
    assert "artifact_data_invalid" in result.content


@pytest.mark.asyncio
async def test_mcp_file_resource_becomes_model_visible_artifact_id(
    artifact_runtime,
    tmp_path,
    monkeypatch,
):
    from personal_agent.mcp.models import MCPCallResult, MCPContentBlock, MCPToolSpec
    from personal_agent.mcp.registrar import MCPToolRegistrar
    from personal_agent.tools import sandbox as sandbox_module

    monkeypatch.setattr(
        sandbox_module,
        "_sandbox",
        sandbox_module.Sandbox([tmp_path], []),
    )

    screenshot = tmp_path / "outbound-multimodal-example.png"
    screenshot.write_bytes(b"playwright-png")

    async def call_tool(_name, _arguments):
        return MCPCallResult(
            text="### Result\n- [Screenshot](./outbound-multimodal-example.png)",
            content=[MCPContentBlock(
                type="resource",
                mime_type="image/png",
                uri=screenshot.resolve().as_uri(),
                metadata={"filename": screenshot.name, "truncated": False},
            )],
        )

    registrar = MCPToolRegistrar("playwright", call_tool)
    registrar.sync([MCPToolSpec(name="browser_take_screenshot")])
    registrar.set_available(True)
    entry = tool_registry.get("mcp__playwright__browser_take_screenshot")
    entry.approval_mode = "auto"
    agent = Agent(_memory_session_key="wechat:user", _hook_turn_id="turn-playwright")
    agent._artifact_store = artifact_runtime
    try:
        result = await execute_tool_call_result(
            {
                "id": "call-playwright",
                "name": "mcp__playwright__browser_take_screenshot",
                "input": {},
            },
            agent=agent,
        )
    finally:
        registrar.unregister_all()

    assert result.status == "success", result.error
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.filename == screenshot.name
    assert artifact.kind == "image"
    assert artifact.mime_type == "image/png"
    assert artifact.artifact_id in format_tool_result(result)
    assert (await artifact_runtime.resolve_path(artifact)).read_bytes() == b"playwright-png"


@pytest.mark.asyncio
async def test_nested_tool_call_does_not_rematerialize_stored_artifact(artifact_runtime):
    agent = Agent(_memory_session_key="wechat:user", _hook_turn_id="turn-nested")
    agent._artifact_store = artifact_runtime

    async def handler():
        return ToolHandlerOutput(
            text="screenshot captured",
            artifacts=[ToolArtifact(
                kind="image",
                name="nested.png",
                mime_type="image/png",
                data=base64.b64encode(b"nested-png").decode(),
            )],
        )

    async def wrapper():
        return await execute_tool_call_result(
            {"id": "inner-call", "name": "nested_artifact_demo", "input": {}},
            agent=agent,
        )

    tool_registry.register(ToolEntry(
        name="nested_artifact_demo",
        description="nested artifact demo",
        schema={},
        handler=handler,
        approval_mode="auto",
    ))
    tool_registry.register(ToolEntry(
        name="nested_artifact_wrapper",
        description="nested artifact wrapper",
        schema={},
        handler=wrapper,
        approval_mode="auto",
    ))
    try:
        result = await execute_tool_call_result(
            {
                "id": "outer-call",
                "name": "nested_artifact_wrapper",
                "input": {},
            },
            agent=agent,
        )
    finally:
        tool_registry.unregister("nested_artifact_wrapper")
        tool_registry.unregister("nested_artifact_demo")

    assert result.status == "success", result.error
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.artifact_id.startswith("art_")
    assert "artifact unavailable" not in result.content
    assert artifact.artifact_id in format_tool_result(result)
    assert (await artifact_runtime.resolve_path(artifact)).read_bytes() == b"nested-png"


@pytest.mark.asyncio
async def test_artifact_from_file_materializes_and_can_be_attached(
    artifact_runtime,
    tmp_path,
    monkeypatch,
):
    from personal_agent.artifacts import TurnResponseDraft
    from personal_agent.plugins.builtin.tools.builtin import artifact_from_file as _module
    from personal_agent.tools import sandbox as sandbox_module

    monkeypatch.setattr(
        sandbox_module,
        "_sandbox",
        sandbox_module.Sandbox([tmp_path], ["**/.env"]),
    )
    report = tmp_path / "report.txt"
    report.write_text("wechat-file", encoding="utf-8")
    agent = Agent(
        _memory_session_key="wechat:user",
        _hook_turn_id="turn-file",
        _artifact_store=artifact_runtime,
        _response_draft=TurnResponseDraft("wechat:user", "turn-file"),
    )

    created = await execute_tool_call_result(
        {
            "id": "artifact-file-1",
            "name": "artifact_from_file",
            "input": {"path": str(report)},
        },
        agent=agent,
    )

    assert created.status == "success", created.error
    assert len(created.artifacts) == 1
    ref = created.artifacts[0]
    assert ref.artifact_id in format_tool_result(created)
    assert ref.kind == "file"
    assert ref.filename == "report.txt"
    assert ref.mime_type == "text/plain"
    assert (await artifact_runtime.resolve_path(ref)).read_text(encoding="utf-8") == "wechat-file"

    attached = await execute_tool_call_result(
        {
            "id": "artifact-file-attach",
            "name": "response_attach",
            "input": {"artifact_ids": [ref.artifact_id]},
        },
        agent=agent,
    )

    assert attached.status == "success", attached.error
    assert [item.artifact_id for item in agent._response_draft.selected] == [ref.artifact_id]


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("outside", "resource_permission_denied"),
        ("blocked", "sandbox_blocked"),
        ("symlink", "artifact_symlink_blocked"),
        ("empty", "artifact_empty"),
        ("large", "artifact_too_large"),
    ],
)
@pytest.mark.asyncio
async def test_artifact_from_file_rejects_unsafe_or_invalid_sources(
    artifact_runtime,
    tmp_path,
    monkeypatch,
    case,
    expected_reason,
):
    from personal_agent.plugins.builtin.tools.builtin import artifact_from_file as _module
    from personal_agent.tools import sandbox as sandbox_module

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    blocked = allowed / ".env"
    blocked.write_text("SECRET=value", encoding="utf-8")
    target = allowed / "target.txt"
    target.write_text("target", encoding="utf-8")
    symlink = allowed / "link.txt"
    symlink.symlink_to(target)
    empty = allowed / "empty.txt"
    empty.touch()
    large = allowed / "large.txt"
    large.write_text("12345", encoding="utf-8")
    paths = {
        "outside": outside,
        "blocked": blocked,
        "symlink": symlink,
        "empty": empty,
        "large": large,
    }
    artifact_runtime.max_file_bytes = 4
    monkeypatch.setattr(
        sandbox_module,
        "_sandbox",
        sandbox_module.Sandbox([allowed], ["**/.env"]),
    )
    agent = Agent(
        _memory_session_key="wechat:user",
        _hook_turn_id=f"turn-{case}",
        _artifact_store=artifact_runtime,
    )

    result = await execute_tool_call_result(
        {
            "id": f"artifact-{case}",
            "name": "artifact_from_file",
            "input": {"path": str(paths[case])},
        },
        agent=agent,
    )

    assert result.status in {"denied", "error"}
    assert result.reason_code == expected_reason
    assert result.artifacts == []


def test_artifact_from_file_declares_exact_read_resource(tmp_path, monkeypatch):
    from personal_agent.plugins.builtin.tools.builtin import artifact_from_file as _module
    from personal_agent.security.evaluator import prepare_tool_call
    from personal_agent.tools import sandbox as sandbox_module

    monkeypatch.setattr(
        sandbox_module,
        "_sandbox",
        sandbox_module.Sandbox([tmp_path], []),
    )
    entry = tool_registry.get("artifact_from_file")
    source = tmp_path / "report.txt"

    prepared = prepare_tool_call(
        {
            "id": "artifact-read-resource",
            "name": "artifact_from_file",
            "input": {"path": str(source)},
        },
        entry,
    )

    assert [resource.as_dict() for resource in prepared.resources] == [{
        "kind": "filesystem",
        "resource": str(source.resolve()),
        "access": "read",
        "reason": "artifact_from_file",
    }]
