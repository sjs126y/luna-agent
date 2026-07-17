from pathlib import Path
from types import SimpleNamespace
import asyncio

import pytest

from personal_agent.db.database import Database
from personal_agent.delivery import (
    DeliveryOutbox,
    DeliveryRequest,
    DeliveryResult,
    DeliveryStatus,
    DeliveryWorker,
)
from personal_agent.models.messages import OutboundMessage


@pytest.mark.asyncio
async def test_outbox_persists_logical_message_across_store_instances(tmp_path: Path):
    db = Database(tmp_path / "state.db")
    await db.initialize()
    outbox = DeliveryOutbox(db)
    request = DeliveryRequest(session_key="wechat:c1:u1", message=OutboundMessage.text("hello"))

    await outbox.enqueue(request)
    restored = await DeliveryOutbox(db).get(request.delivery_id)

    assert restored.request.message.render_text() == "hello"
    assert restored.status == "pending"
    await db.close()


@pytest.mark.asyncio
async def test_outbox_retries_transient_failure_and_worker_completes(tmp_path: Path):
    db = Database(tmp_path / "state.db")
    await db.initialize()
    outbox = DeliveryOutbox(db, max_attempts=3)
    request = DeliveryRequest(session_key="wechat:c1:u1", message=OutboundMessage.text("hello"))
    await outbox.enqueue(request)
    deferred = await outbox.record_result(DeliveryResult(
        delivery_id=request.delivery_id,
        session_key=request.session_key,
        status=DeliveryStatus.FAILED,
        error="connection reset",
    ))
    await db.update_delivery(request.delivery_id, next_attempt_at=0)

    class Service:
        async def deliver_once(self, restored):
            return DeliveryResult(
                delivery_id=restored.delivery_id,
                session_key=restored.session_key,
                status=DeliveryStatus.DELIVERED,
                message_id="m1",
            )

    results = await DeliveryWorker(Service(), outbox).process_due()
    record = await db.delivery_record(request.delivery_id)

    assert deferred.status == DeliveryStatus.DEFERRED
    assert results[0].delivered
    assert record["status"] == "delivered"
    assert record["attempts"] == 2
    await db.close()


@pytest.mark.asyncio
async def test_outbox_does_not_retry_ambiguous_timeout(tmp_path: Path):
    db = Database(tmp_path / "state.db")
    await db.initialize()
    outbox = DeliveryOutbox(db)
    request = DeliveryRequest(session_key="wechat:c1:u1", message=OutboundMessage.text("hello"))
    await outbox.enqueue(request)

    result = await outbox.record_result(DeliveryResult(
        delivery_id=request.delivery_id,
        session_key=request.session_key,
        status=DeliveryStatus.FAILED,
        error="read timeout",
        ambiguous=True,
    ))
    record = await db.delivery_record(request.delivery_id)

    assert result.ambiguous is True
    assert record["status"] == "ambiguous"
    assert await outbox.due() == []
    await db.close()


@pytest.mark.asyncio
async def test_delivery_worker_processes_due_records_in_background(tmp_path: Path):
    db = Database(tmp_path / "state.db")
    await db.initialize()
    outbox = DeliveryOutbox(db)
    request = DeliveryRequest(session_key="wechat:c1:u1", message=OutboundMessage.text("hello"))
    await outbox.enqueue(request)

    class Service:
        async def deliver_once(self, restored):
            return DeliveryResult(
                delivery_id=restored.delivery_id,
                session_key=restored.session_key,
                status=DeliveryStatus.DELIVERED,
            )

    worker = DeliveryWorker(Service(), outbox, poll_interval=0.01)
    worker.start()
    for _ in range(20):
        if (await db.delivery_record(request.delivery_id))["status"] == "delivered":
            break
        await asyncio.sleep(0.01)
    await worker.close()

    assert (await db.delivery_record(request.delivery_id))["status"] == "delivered"
    await db.close()


@pytest.mark.asyncio
async def test_outbox_claim_allows_only_one_sender(tmp_path: Path):
    db = Database(tmp_path / "state.db")
    await db.initialize()
    outbox = DeliveryOutbox(db)
    request = DeliveryRequest(session_key="wechat:c1:u1", message=OutboundMessage.text("hello"))
    await outbox.enqueue(request)

    first, second = await asyncio.gather(
        outbox.claim(request.delivery_id),
        outbox.claim(request.delivery_id),
    )

    assert sorted([first, second]) == [False, True]
    assert (await db.delivery_record(request.delivery_id))["status"] == "sending"
    await db.close()


@pytest.mark.asyncio
async def test_database_recovers_interrupted_sending_record(tmp_path: Path):
    path = tmp_path / "state.db"
    db = Database(path)
    await db.initialize()
    outbox = DeliveryOutbox(db)
    request = DeliveryRequest(session_key="wechat:c1:u1", message=OutboundMessage.text("hello"))
    await outbox.enqueue(request)
    assert await outbox.claim(request.delivery_id)
    await db.close()

    reopened = Database(path)
    await reopened.initialize()
    record = await reopened.delivery_record(request.delivery_id)

    assert record["status"] == "retry"
    assert record["next_attempt_at"] == 0
    await reopened.close()


@pytest.mark.asyncio
async def test_database_recovers_interrupted_sending_part(tmp_path: Path):
    from personal_agent.delivery import DeliveryOperation

    path = tmp_path / "state.db"
    db = Database(path)
    await db.initialize()
    outbox = DeliveryOutbox(db)
    request = DeliveryRequest(session_key="wechat:c1:u1", message=OutboundMessage.text("hello"))
    await outbox.enqueue(request)
    await outbox.ensure_parts(request.delivery_id, (DeliveryOperation(0, "text", text="hello"),))
    await outbox.start_part(request.delivery_id, 0)
    await db.close()

    reopened = Database(path)
    await reopened.initialize()
    parts = await DeliveryOutbox(reopened).parts(request.delivery_id)

    assert parts[0].status == "retry"
    await reopened.close()
