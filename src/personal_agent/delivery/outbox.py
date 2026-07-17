"""SQLite-backed durable delivery queue."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import time

from personal_agent.delivery.models import (
    DeliveryKind,
    DeliveryPartResult,
    DeliveryRequest,
    DeliveryResult,
    DeliveryStatus,
)
from personal_agent.delivery.planner import DeliveryOperation
from personal_agent.models.messages import MessagePart, OutboundMessage


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    request: DeliveryRequest
    status: str
    attempts: int
    next_attempt_at: float


@dataclass(frozen=True, slots=True)
class OutboxPartRecord:
    delivery_id: str
    operation: DeliveryOperation
    status: str
    attempts: int
    message_id: str = ""
    platform_file_id: str = ""
    error: str = ""
    ambiguous: bool = False


class DeliveryOutbox:
    def __init__(self, db, *, max_attempts: int = 3) -> None:
        self.db = db
        self.max_attempts = max(1, int(max_attempts))

    async def enqueue(self, request: DeliveryRequest) -> None:
        now = time.time()
        await self.db.enqueue_delivery((
            request.delivery_id,
            request.session_key,
            request.kind.value,
            json.dumps(request.message.as_dict(), ensure_ascii=False),
            json.dumps(request.metadata, ensure_ascii=False),
            "pending",
            0,
            now,
            now,
            now,
        ))

    async def due(self, *, limit: int = 50) -> list[OutboxRecord]:
        rows = await self.db.due_deliveries(now=time.time(), limit=limit)
        return [self._record(row) for row in rows]

    async def get(self, delivery_id: str) -> OutboxRecord | None:
        row = await self.db.delivery_record(delivery_id)
        return self._record(row) if row else None

    async def claim(self, delivery_id: str) -> bool:
        return await self.db.claim_delivery(delivery_id, updated_at=time.time())

    async def ensure_parts(
        self,
        delivery_id: str,
        operations: tuple[DeliveryOperation, ...],
    ) -> list[OutboxPartRecord]:
        await self.db.ensure_delivery_parts(
            delivery_id,
            [operation.as_dict() for operation in operations],
            updated_at=time.time(),
        )
        return await self.parts(delivery_id)

    async def parts(self, delivery_id: str) -> list[OutboxPartRecord]:
        rows = await self.db.delivery_part_records(delivery_id)
        result = []
        for row in rows:
            result.append(OutboxPartRecord(
                delivery_id=str(row["delivery_id"]),
                operation=DeliveryOperation.from_dict(json.loads(row["operation_json"] or "{}")),
                status=str(row["status"] or "pending"),
                attempts=int(row["attempts"] or 0),
                message_id=str(row["message_id"] or ""),
                platform_file_id=str(row["platform_file_id"] or ""),
                error=str(row["last_error"] or ""),
                ambiguous=bool(row["ambiguous"]),
            ))
        return result

    async def record_part_result(self, delivery_id: str, result: DeliveryPartResult) -> OutboxPartRecord:
        records = await self.parts(delivery_id)
        current = next((item for item in records if item.operation.index == result.index), None)
        attempts = (current.attempts if current else 0) + (0 if result.skipped else 1)
        status = (
            "delivered" if result.success else
            "ambiguous" if result.ambiguous else
            "retry" if attempts < self.max_attempts else
            "failed"
        )
        await self.db.update_delivery_part(
            delivery_id,
            result.index,
            status=status,
            attempts=attempts,
            message_id=result.message_id,
            last_error=result.error,
            ambiguous=int(result.ambiguous),
            updated_at=time.time(),
        )
        updated = await self.parts(delivery_id)
        return next(item for item in updated if item.operation.index == result.index)

    async def start_part(self, delivery_id: str, part_index: int) -> None:
        await self.db.update_delivery_part(
            delivery_id,
            part_index,
            status="sending",
            updated_at=time.time(),
        )

    async def record_result(self, result: DeliveryResult) -> DeliveryResult:
        record = await self.get(result.delivery_id)
        attempts = (record.attempts if record else 0) + 1
        terminal = result.delivered or result.status == DeliveryStatus.SUPPRESSED
        ambiguous = result.ambiguous or "timeout" in result.error.lower()
        retry = not terminal and not ambiguous and attempts < self.max_attempts
        status = (
            "delivered" if result.delivered else
            "suppressed" if result.status == DeliveryStatus.SUPPRESSED else
            "ambiguous" if ambiguous else
            "retry" if retry else
            "failed"
        )
        next_attempt = time.time() + min(60.0, 2 ** max(0, attempts - 1)) if retry else 0.0
        await self.db.update_delivery(
            result.delivery_id,
            status=status,
            attempts=attempts,
            next_attempt_at=next_attempt,
            platform=result.platform,
            chat_id=result.chat_id,
            message_id=result.message_id,
            last_error=result.error,
            updated_at=time.time(),
        )
        if retry:
            return DeliveryResult(
                delivery_id=result.delivery_id,
                session_key=result.session_key,
                status=DeliveryStatus.DEFERRED,
                platform=result.platform,
                chat_id=result.chat_id,
                error=result.error,
                attempts=attempts,
                ambiguous=ambiguous,
                partial=result.partial,
                degraded=result.degraded,
                parts=result.parts,
            )
        return DeliveryResult(
            delivery_id=result.delivery_id,
            session_key=result.session_key,
            status=result.status,
            platform=result.platform,
            chat_id=result.chat_id,
            message_id=result.message_id,
            error=result.error,
            attempts=attempts,
            ambiguous=ambiguous,
            partial=result.partial,
            degraded=result.degraded,
            parts=result.parts,
        )

    @staticmethod
    def _record(row: dict) -> OutboxRecord:
        message_data = json.loads(row["message_json"] or "{}")
        message = OutboundMessage(parts=[MessagePart(**part) for part in message_data.get("parts", [])])
        request = DeliveryRequest(
            delivery_id=row["delivery_id"],
            session_key=row["session_key"],
            kind=DeliveryKind(row["kind"]),
            message=message,
            metadata=json.loads(row["metadata_json"] or "{}"),
        )
        return OutboxRecord(
            request=request,
            status=row["status"],
            attempts=int(row["attempts"] or 0),
            next_attempt_at=float(row["next_attempt_at"] or 0),
        )


class DeliveryWorker:
    def __init__(self, service, outbox: DeliveryOutbox, *, poll_interval: float = 1.0) -> None:
        self.service = service
        self.outbox = outbox
        self._lock = asyncio.Lock()
        self.poll_interval = max(0.05, float(poll_interval))
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="delivery-outbox")

    async def close(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            await task

    async def process_due(self, *, limit: int = 50) -> list[DeliveryResult]:
        async with self._lock:
            results = []
            for record in await self.outbox.due(limit=limit):
                if not await self.outbox.claim(record.request.delivery_id):
                    continue
                result = await self.service.deliver_once(record.request)
                results.append(await self.outbox.record_result(result))
            return results

    async def _run(self) -> None:
        while not self._stop.is_set():
            await self.process_due()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass
