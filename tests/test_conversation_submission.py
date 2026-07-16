"""Conversation submission contract behavior."""

from __future__ import annotations

import asyncio

import pytest

from personal_agent.conversation import (
    ResponseMode,
    SubmissionHandle,
    SubmissionOrigin,
    SubmissionOutcome,
    SubmissionReceipt,
    SubmissionRequest,
    SubmissionStatus,
)
from personal_agent.models.messages import SessionSource


def test_submission_request_normalizes_identity_and_copies_metadata():
    metadata = {"platform_message_id": "message-1"}
    request = SubmissionRequest.text(
        session_key=" gateway:wechat:user ",
        text="hello",
        origin=SubmissionOrigin.GATEWAY,
        response_mode=ResponseMode.DELIVER,
        source=SessionSource(platform="wechat", user_id="user"),
        metadata=metadata,
    )
    metadata["changed"] = True

    assert request.session_key == "gateway:wechat:user"
    assert request.input.text == "hello"
    assert request.input.source.platform == "wechat"
    assert request.metadata == {"platform_message_id": "message-1"}
    assert request.request_id.startswith("sub_")


@pytest.mark.parametrize("session_key", ["", "  "])
def test_submission_request_requires_session_key(session_key):
    with pytest.raises(ValueError, match="session_key"):
        SubmissionRequest.text(
            session_key=session_key,
            text="hello",
            origin=SubmissionOrigin.CLI,
            response_mode=ResponseMode.RETURN_ONLY,
        )


@pytest.mark.asyncio
async def test_submission_handle_exposes_receipt_and_final_outcome():
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    receipt = SubmissionReceipt(
        request_id="sub_1",
        session_key="cli:default:local",
        accepted=True,
        status=SubmissionStatus.ACCEPTED,
        queue_position=1,
    )
    handle = SubmissionHandle(receipt, future)
    outcome = SubmissionOutcome(
        request_id="sub_1",
        session_key="cli:default:local",
        status=SubmissionStatus.COMPLETED,
        response="done",
    )
    future.set_result(outcome)

    assert handle.request_id == "sub_1"
    assert handle.done is True
    assert await handle.outcome() == outcome
    assert outcome.succeeded is True
