from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from luna_agent.conversation import (
    ConversationCoordinator,
    DurableSubmissionLedger,
    ResponseMode,
    SubmissionOrigin,
    SubmissionOutcome,
    SubmissionRequest,
    SubmissionStatus,
)
from luna_agent.db.database import Database
from luna_agent.delivery import DeliveryResult, DeliveryStatus


class RecordingService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run_turn_input_events(self, session_key, user_input, **kwargs):
        self.calls.append(user_input.text)
        return SimpleNamespace(
            status="completed",
            final_response=f"echo:{user_input.text}",
            error="",
            outbound_message=None,
        )


class RecordingDelivery:
    def __init__(self, status=DeliveryStatus.DELIVERED) -> None:
        self.status = status
        self.requests = []

    async def deliver(self, request):
        self.requests.append(request)
        return DeliveryResult(
            delivery_id=request.delivery_id,
            session_key=request.session_key,
            status=self.status,
        )


def durable_request(text: str = "hello", *, response_mode=ResponseMode.RETURN_ONLY):
    return SubmissionRequest.text(
        session_key="plugin-session",
        text=text,
        origin=SubmissionOrigin.PLUGIN,
        response_mode=response_mode,
        owner_id="automation/demo",
        durable=True,
    )


@pytest.mark.asyncio
async def test_durable_submission_reuses_result_after_coordinator_restart(tmp_path):
    db = Database(tmp_path / "state.db")
    await db.initialize()
    ledger = DurableSubmissionLedger(db)
    service = RecordingService()
    request = durable_request()
    request = SubmissionRequest(
        session_key=request.session_key,
        input=request.input,
        origin=request.origin,
        response_mode=request.response_mode,
        request_id="event:stable-1",
        owner_id=request.owner_id,
        durable=True,
    )

    first = ConversationCoordinator(service, submission_ledger=ledger)
    assert (await (await first.submit(request)).outcome()).response == "echo:hello"
    await first.close()

    restarted_service = RecordingService()
    restarted = ConversationCoordinator(restarted_service, submission_ledger=ledger)
    duplicate = await restarted.submit(request)
    outcome = await duplicate.outcome()

    assert outcome.status == SubmissionStatus.COMPLETED
    assert outcome.response == "echo:hello"
    assert outcome.payload["idempotent_replay"] is True
    assert restarted_service.calls == []
    await restarted.close()
    await db.close()


@pytest.mark.asyncio
async def test_durable_submission_rejects_same_id_with_changed_payload(tmp_path):
    db = Database(tmp_path / "state.db")
    await db.initialize()
    ledger = DurableSubmissionLedger(db)
    service = RecordingService()
    coordinator = ConversationCoordinator(service, submission_ledger=ledger)
    first = durable_request("first")
    first = SubmissionRequest(
        session_key=first.session_key,
        input=first.input,
        origin=first.origin,
        response_mode=first.response_mode,
        request_id="event:conflict",
        owner_id=first.owner_id,
        durable=True,
    )
    changed = durable_request("changed")
    changed = SubmissionRequest(
        session_key=changed.session_key,
        input=changed.input,
        origin=changed.origin,
        response_mode=changed.response_mode,
        request_id=first.request_id,
        owner_id=changed.owner_id,
        durable=True,
    )

    await (await coordinator.submit(first)).outcome()
    coordinator._submissions.clear()
    outcome = await (await coordinator.submit(changed)).outcome()

    assert outcome.status == SubmissionStatus.REJECTED
    assert "payload" in outcome.error
    assert service.calls == ["first"]
    await coordinator.close()
    await db.close()


@pytest.mark.asyncio
async def test_conversation_checkpoint_resumes_delivery_without_model_rerun(tmp_path):
    db = Database(tmp_path / "state.db")
    await db.initialize()
    ledger = DurableSubmissionLedger(db)
    request = durable_request(response_mode=ResponseMode.DELIVER)
    request = SubmissionRequest(
        session_key=request.session_key,
        input=request.input,
        origin=request.origin,
        response_mode=request.response_mode,
        request_id="event:resume-delivery",
        owner_id=request.owner_id,
        durable=True,
    )
    await ledger.claim(request)
    await ledger.store_conversation(request, SubmissionOutcome(
        request_id=request.request_id,
        session_key=request.session_key,
        status=SubmissionStatus.COMPLETED,
        response="persisted response",
    ))
    service = RecordingService()
    delivery = RecordingDelivery()
    coordinator = ConversationCoordinator(
        service,
        delivery_service=delivery,
        submission_ledger=ledger,
    )

    outcome = await (await coordinator.submit(request)).outcome()

    assert outcome.status == SubmissionStatus.COMPLETED
    assert outcome.response == "persisted response"
    assert service.calls == []
    assert len(delivery.requests) == 1
    assert delivery.requests[0].delivery_id == ledger.delivery_id(request)
    await coordinator.close()
    await db.close()


@pytest.mark.asyncio
async def test_interrupted_claim_becomes_retryable_after_database_reopen(tmp_path):
    path = tmp_path / "state.db"
    db = Database(path)
    await db.initialize()
    request = durable_request()
    request = SubmissionRequest(
        session_key=request.session_key,
        input=request.input,
        origin=request.origin,
        response_mode=request.response_mode,
        request_id="event:interrupted",
        owner_id=request.owner_id,
        durable=True,
    )
    ledger = DurableSubmissionLedger(db)
    await ledger.claim(request)
    await ledger.mark_running(request, turn_id="turn_interrupted")
    await db.close()

    reopened = Database(path)
    await reopened.initialize()
    service = RecordingService()
    coordinator = ConversationCoordinator(
        service,
        submission_ledger=DurableSubmissionLedger(reopened),
    )
    outcome = await (await coordinator.submit(request)).outcome()

    assert outcome.status == SubmissionStatus.COMPLETED
    assert service.calls == ["hello"]
    await coordinator.close()
    await reopened.close()


@pytest.mark.asyncio
async def test_only_one_concurrent_ledger_claim_owns_execution(tmp_path):
    db = Database(tmp_path / "state.db")
    await db.initialize()
    ledger = DurableSubmissionLedger(db)
    request = durable_request()
    request = SubmissionRequest(
        session_key=request.session_key,
        input=request.input,
        origin=request.origin,
        response_mode=request.response_mode,
        request_id="event:concurrent",
        owner_id=request.owner_id,
        durable=True,
    )

    first, second = await asyncio.gather(
        ledger.claim(request),
        ledger.claim(request),
    )

    assert sorted([first.kind.value, second.kind.value]) == ["active", "new"]
    await db.close()
