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
