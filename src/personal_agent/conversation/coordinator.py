"""Queueing boundary for all conversation runtime entrypoints."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from personal_agent.conversation.submission import (
    SubmissionHandle,
    SubmissionOutcome,
    SubmissionReceipt,
    SubmissionRequest,
    SubmissionStatus,
)


class ConversationTurnRunner(Protocol):
    async def run_turn_input_events(
        self,
        session_key: str,
        user_input,
        *,
        event_sink=None,
        confirm=None,
    ): ...


@dataclass(slots=True)
class _QueuedSubmission:
    request: SubmissionRequest
    future: asyncio.Future[SubmissionOutcome]


class ConversationCoordinator:
    """Serializes turns per session while allowing independent sessions to run."""

    def __init__(self, conversation_service: ConversationTurnRunner) -> None:
        self.conversation_service = conversation_service
        self._queues: dict[str, asyncio.Queue[_QueuedSubmission]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._active: dict[str, SubmissionRequest] = {}
        self._lock = asyncio.Lock()
        self._accepting = True

    async def submit(self, request: SubmissionRequest) -> SubmissionHandle:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[SubmissionOutcome] = loop.create_future()
        async with self._lock:
            if not self._accepting:
                receipt = SubmissionReceipt(
                    request_id=request.request_id,
                    session_key=request.session_key,
                    accepted=False,
                    status=SubmissionStatus.REJECTED,
                    reason="conversation coordinator is shutting down",
                )
                future.set_result(
                    SubmissionOutcome(
                        request_id=request.request_id,
                        session_key=request.session_key,
                        status=SubmissionStatus.REJECTED,
                        error=receipt.reason,
                    )
                )
                return SubmissionHandle(receipt, future)

            queue = self._queues.setdefault(request.session_key, asyncio.Queue())
            position = queue.qsize() + (1 if request.session_key in self._active else 0) + 1
            queue.put_nowait(_QueuedSubmission(request=request, future=future))
            worker = self._workers.get(request.session_key)
            if worker is None or worker.done():
                self._workers[request.session_key] = asyncio.create_task(
                    self._run_session(request.session_key, queue),
                    name=f"conversation:{request.session_key}",
                )

        receipt = SubmissionReceipt(
            request_id=request.request_id,
            session_key=request.session_key,
            accepted=True,
            status=SubmissionStatus.ACCEPTED,
            queue_position=position,
        )
        return SubmissionHandle(receipt, future)

    async def close(self, *, cancel_pending: bool = False) -> None:
        async with self._lock:
            self._accepting = False
            if cancel_pending:
                for queue in self._queues.values():
                    self._cancel_queued(queue)
            workers = list(self._workers.values())
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    def snapshot(self) -> dict[str, Any]:
        sessions = sorted(set(self._queues) | set(self._active))
        return {
            "accepting": self._accepting,
            "active_count": len(self._active),
            "queued_count": sum(queue.qsize() for queue in self._queues.values()),
            "sessions": [
                {
                    "session_key": session_key,
                    "active_request_id": getattr(self._active.get(session_key), "request_id", ""),
                    "queued_count": self._queues.get(session_key).qsize()
                    if session_key in self._queues
                    else 0,
                }
                for session_key in sessions
            ],
        }

    async def _run_session(
        self,
        session_key: str,
        queue: asyncio.Queue[_QueuedSubmission],
    ) -> None:
        while True:
            item = await queue.get()
            try:
                if not item.future.cancelled():
                    self._active[session_key] = item.request
                    outcome = await self._execute(item.request)
                    if not item.future.done():
                        item.future.set_result(outcome)
            finally:
                self._active.pop(session_key, None)
                queue.task_done()

            async with self._lock:
                if queue.empty():
                    self._queues.pop(session_key, None)
                    self._workers.pop(session_key, None)
                    return

    async def _execute(self, request: SubmissionRequest) -> SubmissionOutcome:
        started_at = datetime.now(UTC)
        try:
            result = await self.conversation_service.run_turn_input_events(
                request.session_key,
                request.input,
                event_sink=request.event_sink,
                confirm=request.confirm,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return SubmissionOutcome(
                request_id=request.request_id,
                session_key=request.session_key,
                status=SubmissionStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                started_at=started_at,
            )

        result_status = str(getattr(result, "status", "completed") or "completed")
        status = (
            SubmissionStatus.COMPLETED
            if result_status == "completed"
            else SubmissionStatus.CANCELLED
            if result_status == "stopped"
            else SubmissionStatus.FAILED
        )
        return SubmissionOutcome(
            request_id=request.request_id,
            session_key=request.session_key,
            status=status,
            response=str(getattr(result, "final_response", "") or ""),
            error=str(getattr(result, "error", "") or ""),
            payload={"turn_result": result},
            started_at=started_at,
        )

    @staticmethod
    def _cancel_queued(queue: asyncio.Queue[_QueuedSubmission]) -> None:
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not item.future.done():
                item.future.cancel()
            queue.task_done()
