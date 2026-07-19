from __future__ import annotations

import pytest
import pytest_asyncio

from luna_agent.agent.agent import Agent
from luna_agent.artifacts import ArtifactStore, TurnResponseDraft
from luna_agent.conversation.service import _outbound_message_for_turn
from luna_agent.db.database import Database
from luna_agent.plugins.builtin.tools.builtin.response_attach import response_attach
from luna_agent.tools.runtime_context import reset_current_tool_agent, set_current_tool_agent


@pytest_asyncio.fixture
async def response_runtime(tmp_path):
    db = Database(tmp_path / "state.db")
    await db.initialize()
    store = ArtifactStore(tmp_path / "artifacts", db)
    await store.initialize()
    yield store
    await db.close()


@pytest.mark.asyncio
async def test_response_attach_selects_current_turn_artifact(response_runtime):
    ref = await response_runtime.create(
        b"image",
        kind="image",
        filename="result.png",
        mime_type="image/png",
        session_key="wechat:user",
        turn_id="turn-1",
    )
    agent = Agent(
        _memory_session_key="wechat:user",
        _hook_turn_id="turn-1",
        _artifact_store=response_runtime,
        _response_draft=TurnResponseDraft("wechat:user", "turn-1"),
    )
    token = set_current_tool_agent(agent)
    try:
        result = await response_attach([ref.artifact_id, ref.artifact_id])
    finally:
        reset_current_tool_agent(token)

    assert result.is_error is False
    assert len(agent._response_draft.selected) == 1
    message = _outbound_message_for_turn("这是结果。", agent, include_artifacts=True)
    assert message.render_text() == "这是结果。[image: result.png]"
    assert message.parts[1].artifact_id == ref.artifact_id


@pytest.mark.asyncio
async def test_response_attach_rejects_other_turn_artifact(response_runtime):
    ref = await response_runtime.create(
        b"file",
        kind="file",
        filename="report.txt",
        mime_type="text/plain",
        session_key="wechat:user",
        turn_id="turn-old",
    )
    agent = Agent(
        _memory_session_key="wechat:user",
        _hook_turn_id="turn-new",
        _artifact_store=response_runtime,
        _response_draft=TurnResponseDraft("wechat:user", "turn-new"),
    )
    token = set_current_tool_agent(agent)
    try:
        result = await response_attach([ref.artifact_id])
    finally:
        reset_current_tool_agent(token)

    assert result.is_error is True
    assert result.metadata["reason_code"] == "artifact_scope_mismatch"
    assert agent._response_draft.selected == []


def test_stopped_turn_does_not_include_selected_artifacts():
    agent = Agent()
    agent._response_draft = TurnResponseDraft("cli:user", "turn-1", selected=[])
    message = _outbound_message_for_turn("已停止。", agent, include_artifacts=False)
    assert [part.type for part in message.parts] == ["text"]


@pytest.mark.asyncio
async def test_response_attach_through_executor_emits_selection_event(response_runtime):
    from luna_agent.conversation.events import EventRecorder
    from luna_agent.tools.executor import execute_tool_call_result

    ref = await response_runtime.create(
        b"report",
        kind="file",
        filename="report.txt",
        mime_type="text/plain",
        session_key="wechat:user",
        turn_id="turn-1",
    )
    agent = Agent(
        _memory_session_key="wechat:user",
        _hook_turn_id="turn-1",
        _artifact_store=response_runtime,
        _response_draft=TurnResponseDraft("wechat:user", "turn-1"),
    )
    events = EventRecorder()

    result = await execute_tool_call_result(
        {
            "id": "attach-1",
            "name": "response_attach",
            "input": {"artifact_ids": [ref.artifact_id]},
        },
        agent=agent,
        event_sink=events,
    )

    assert result.status == "success"
    selected = [event for event in events.events if event.type == "response_artifact_selected"]
    assert selected[0].data == {"artifact_ids": [ref.artifact_id], "count": 1}
