"""Conversation coordinator queue and lifecycle behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from personal_agent.conversation import (
    ConversationCoordinator,
    ResponseMode,
    SubmissionOrigin,
    SubmissionRequest,
    SubmissionStatus,
)
from personal_agent.commands.runtime import CommandResult
from personal_agent.delivery import DeliveryResult, DeliveryStatus


def _request(session_key: str, text: str) -> SubmissionRequest:
    return SubmissionRequest.text(
        session_key=session_key,
        text=text,
        origin=SubmissionOrigin.CLI,
        response_mode=ResponseMode.RETURN_ONLY,
    )


class RecordingService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.running = 0
        self.max_running = 0
        self.release = asyncio.Event()
        self.release.set()

    async def run_turn_input_events(self, session_key, user_input, **kwargs):
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        self.calls.append((session_key, user_input.text))
        await self.release.wait()
        self.running -= 1
        if user_input.text == "fail":
            raise RuntimeError("broken turn")
        return SimpleNamespace(
            status="completed",
            final_response=f"echo:{user_input.text}",
            error="",
        )


class TurnAwareService(RecordingService):
    def __init__(self) -> None:
        super().__init__()
        self.turns = []

    async def run_turn_input_events(self, session_key, user_input, **kwargs):
        steer = kwargs["steer"]
        turn_id = kwargs["turn_id"]
        self.turns.append((turn_id, steer.active_turn(session_key)))
        return await super().run_turn_input_events(session_key, user_input, **kwargs)

    def capture_turn_policy(self, session_key):
        return SimpleNamespace(session_key=session_key, revision=7)


@pytest.mark.asyncio
async def test_coordinator_serializes_same_session_in_submission_order():
    service = RecordingService()
    service.release.clear()
    coordinator = ConversationCoordinator(service)

    first = await coordinator.submit(_request("session", "first"))
    second = await coordinator.submit(_request("session", "second"))
    await asyncio.sleep(0)

    assert service.calls == [("session", "first")]
    assert first.receipt.queue_position == 1
    assert second.receipt.queue_position == 2
    service.release.set()
    assert (await first.outcome()).response == "echo:first"
    assert (await second.outcome()).response == "echo:second"
    await coordinator.close()


@pytest.mark.asyncio
async def test_coordinator_runs_different_sessions_concurrently():
    service = RecordingService()
    service.release.clear()
    coordinator = ConversationCoordinator(service)

    first = await coordinator.submit(_request("one", "first"))
    second = await coordinator.submit(_request("two", "second"))
    await asyncio.sleep(0)

    assert service.max_running == 2
    service.release.set()
    await asyncio.gather(first.outcome(), second.outcome())
    await coordinator.close()


@pytest.mark.asyncio
async def test_coordinator_skips_cancelled_queued_submission():
    service = RecordingService()
    service.release.clear()
    coordinator = ConversationCoordinator(service)

    first = await coordinator.submit(_request("session", "first"))
    second = await coordinator.submit(_request("session", "second"))
    assert second.cancel() is True
    service.release.set()

    await first.outcome()
    with pytest.raises(asyncio.CancelledError):
        await second.outcome()
    assert service.calls == [("session", "first")]
    await coordinator.close()


@pytest.mark.asyncio
async def test_failed_turn_does_not_block_next_submission():
    service = RecordingService()
    coordinator = ConversationCoordinator(service)

    failed = await coordinator.submit(_request("session", "fail"))
    next_turn = await coordinator.submit(_request("session", "next"))

    assert (await failed.outcome()).status == SubmissionStatus.FAILED
    assert (await next_turn.outcome()).status == SubmissionStatus.COMPLETED
    assert service.calls == [("session", "fail"), ("session", "next")]
    await coordinator.close()


@pytest.mark.asyncio
async def test_coordinator_owns_active_turn_lifecycle():
    service = TurnAwareService()
    coordinator = ConversationCoordinator(service)

    handle = await coordinator.submit(_request("session", "hello"))
    await handle.outcome()

    turn_id, active = service.turns[0]
    assert active.turn_id == turn_id
    assert active.request_id == handle.request_id
    assert coordinator.active_turns.active_turn("session") is None
    await coordinator.close()


@pytest.mark.asyncio
async def test_control_command_bypasses_busy_session_queue():
    service = RecordingService()
    service.release.clear()
    commands = []

    async def dispatch(request):
        commands.append(request.input.text)
        return CommandResult.reply("stopped")

    coordinator = ConversationCoordinator(service, command_dispatcher=dispatch)
    running = await coordinator.submit(_request("session", "work"))
    await asyncio.sleep(0)
    stop = await coordinator.submit(_request("session", "/stop"))

    stop_outcome = await stop.outcome()
    assert stop.receipt.queue_position == 0
    assert stop_outcome.response == "stopped"
    assert stop_outcome.kind.value == "control"
    assert commands == ["/stop"]

    service.release.set()
    await running.outcome()
    await coordinator.close()


@pytest.mark.asyncio
async def test_skill_command_forward_is_ordered_as_conversation():
    service = RecordingService()

    async def dispatch(request):
        return CommandResult.continue_with("expanded skill prompt")

    coordinator = ConversationCoordinator(service, command_dispatcher=dispatch)
    handle = await coordinator.submit(_request("session", "/skill"))

    outcome = await handle.outcome()
    assert outcome.response == "echo:expanded skill prompt"
    assert service.calls == [("session", "expanded skill prompt")]
    await coordinator.close()


@pytest.mark.asyncio
async def test_deliver_response_mode_aggregates_delivery_result():
    service = RecordingService()

    class Delivery:
        async def deliver(self, request):
            assert request.message.render_text() == "echo:hello"
            return DeliveryResult(
                delivery_id=request.delivery_id,
                session_key=request.session_key,
                status=DeliveryStatus.DELIVERED,
                platform="wechat",
                chat_id="c1",
                message_id="m1",
                attempts=1,
            )

    coordinator = ConversationCoordinator(service, delivery_service=Delivery())
    request = SubmissionRequest.text(
        session_key="session",
        text="hello",
        origin=SubmissionOrigin.GATEWAY,
        response_mode=ResponseMode.DELIVER,
    )

    outcome = await (await coordinator.submit(request)).outcome()

    assert outcome.status == SubmissionStatus.COMPLETED
    assert outcome.payload["delivery_result"].message_id == "m1"
    await coordinator.close()
