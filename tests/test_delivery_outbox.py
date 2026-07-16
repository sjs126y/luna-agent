from pathlib import Path
from types import SimpleNamespace

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
