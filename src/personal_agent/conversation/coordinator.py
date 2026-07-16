"""Queueing boundary for all conversation runtime entrypoints."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
import uuid

from personal_agent.commands.policy import CommandExecutionPolicy, command_execution_policy
from personal_agent.commands.runtime import CommandResult
from personal_agent.conversation.input import ConversationInput
from personal_agent.conversation.steer import ActiveTurnRegistry

from personal_agent.conversation.submission import (
    SubmissionHandle,
    SubmissionKind,
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
        turn_id: str = "",
        steer=None,
        policy_snapshot=None,
    ): ...


class CoordinatorCommandDispatcher(Protocol):
    async def __call__(self, request: SubmissionRequest) -> CommandResult: ...


@dataclass(slots=True)
class _QueuedSubmission:
    request: SubmissionRequest
    future: asyncio.Future[SubmissionOutcome]


class ConversationCoordinator:
    """Serializes turns per session while allowing independent sessions to run."""

    def __init__(
        self,
        conversation_service: ConversationTurnRunner,
        *,
        active_turns: ActiveTurnRegistry | None = None,
        command_dispatcher: CoordinatorCommandDispatcher | None = None,
    ) -> None:
        self.conversation_service = conversation_service
        self.active_turns = active_turns or ActiveTurnRegistry()
        self.command_dispatcher = command_dispatcher
        self._queues: dict[str, asyncio.Queue[_QueuedSubmission]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._immediate_tasks: set[asyncio.Task[None]] = set()
        self._active: dict[str, SubmissionRequest] = {}
        self._lock = asyncio.Lock()
        self._accepting = True

    async def submit(self, request: SubmissionRequest) -> SubmissionHandle:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[SubmissionOutcome] = loop.create_future()
        policy = command_execution_policy(request.input.text) if self.command_dispatcher else None
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

            if policy is not None and policy != CommandExecutionPolicy.BARRIER:
                task = asyncio.create_task(
                    self._complete_immediate(request, future, policy),
                    name=f"command:{request.request_id}",
                )
                self._immediate_tasks.add(task)
                task.add_done_callback(self._immediate_tasks.discard)
                receipt = SubmissionReceipt(
                    request_id=request.request_id,
                    session_key=request.session_key,
                    accepted=True,
                    status=SubmissionStatus.ACCEPTED,
                    queue_position=0,
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
            immediate_tasks = list(self._immediate_tasks)
        tasks = [*workers, *immediate_tasks]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def snapshot(self) -> dict[str, Any]:
        sessions = sorted(set(self._queues) | set(self._active))
        return {
            "accepting": self._accepting,
            "active_count": len(self._active),
            "queued_count": sum(queue.qsize() for queue in self._queues.values()),
            "immediate_count": len(self._immediate_tasks),
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
        command_result = await self._dispatch_command(request)
        if command_result is not None and command_result.continue_text is None:
            return self._command_outcome(request, command_result, started_at=started_at)
        if command_result is not None and command_result.continue_text is not None:
            request = self._with_text(request, command_result.continue_text)
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        capture_policy = getattr(self.conversation_service, "capture_turn_policy", None)
        policy_snapshot = capture_policy(request.session_key) if capture_policy else None
        self.active_turns.begin_turn(
            request.session_key,
            turn_id,
            request_id=request.request_id,
            task=asyncio.current_task(),
        )
        try:
            result = await self.conversation_service.run_turn_input_events(
                request.session_key,
                request.input,
                event_sink=request.event_sink,
                confirm=request.confirm,
                turn_id=turn_id,
                steer=self.active_turns,
                policy_snapshot=policy_snapshot,
            )
        except asyncio.CancelledError:
            return SubmissionOutcome(
                request_id=request.request_id,
                session_key=request.session_key,
                status=SubmissionStatus.CANCELLED,
                error="conversation turn cancelled",
                started_at=started_at,
            )
        except Exception as exc:
            return SubmissionOutcome(
                request_id=request.request_id,
                session_key=request.session_key,
                status=SubmissionStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                started_at=started_at,
            )
        finally:
            self.active_turns.end_turn(request.session_key, turn_id)

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

    async def _complete_immediate(
        self,
        request: SubmissionRequest,
        future: asyncio.Future[SubmissionOutcome],
        policy: CommandExecutionPolicy,
    ) -> None:
        started_at = datetime.now(UTC)
        try:
            result = await self._dispatch_command(request)
            if result is None:
                outcome = SubmissionOutcome(
                    request_id=request.request_id,
                    session_key=request.session_key,
                    status=SubmissionStatus.REJECTED,
                    kind=SubmissionKind.CONTROL
                    if policy == CommandExecutionPolicy.CONTROL
                    else SubmissionKind.COMMAND,
                    error="slash command was not handled",
                    started_at=started_at,
                )
            elif result.continue_text is not None:
                outcome = SubmissionOutcome(
                    request_id=request.request_id,
                    session_key=request.session_key,
                    status=SubmissionStatus.REJECTED,
                    kind=SubmissionKind.COMMAND,
                    error="forwarding commands must use the conversation queue",
                    started_at=started_at,
                )
            else:
                outcome = self._command_outcome(
                    request,
                    result,
                    started_at=started_at,
                    policy=policy,
                )
        except Exception as exc:
            outcome = SubmissionOutcome(
                request_id=request.request_id,
                session_key=request.session_key,
                status=SubmissionStatus.FAILED,
                kind=SubmissionKind.COMMAND,
                error=f"{type(exc).__name__}: {exc}",
                started_at=started_at,
            )
        if not future.done():
            future.set_result(outcome)

    async def _dispatch_command(self, request: SubmissionRequest) -> CommandResult | None:
        if self.command_dispatcher is None or command_execution_policy(request.input.text) is None:
            return None
        result = await self.command_dispatcher(request)
        return result if result.handled else None

    @staticmethod
    def _command_outcome(
        request: SubmissionRequest,
        result: CommandResult,
        *,
        started_at: datetime,
        policy: CommandExecutionPolicy | None = None,
    ) -> SubmissionOutcome:
        return SubmissionOutcome(
            request_id=request.request_id,
            session_key=request.session_key,
            status=SubmissionStatus.FAILED if result.error else SubmissionStatus.COMPLETED,
            kind=SubmissionKind.CONTROL
            if policy == CommandExecutionPolicy.CONTROL
            else SubmissionKind.COMMAND,
            response=str(result.response or ""),
            error=str(result.error or ""),
            payload=dict(result.payload or {}),
            started_at=started_at,
        )

    @staticmethod
    def _with_text(request: SubmissionRequest, text: str) -> SubmissionRequest:
        user_input = ConversationInput(
            text=text,
            source=request.input.source,
            parts=list(request.input.parts),
            attachments=list(request.input.attachments),
            envelope=request.input.envelope,
            metadata=dict(request.input.metadata),
        )
        return SubmissionRequest(
            session_key=request.session_key,
            input=user_input,
            origin=request.origin,
            response_mode=request.response_mode,
            request_id=request.request_id,
            owner_id=request.owner_id,
            metadata=request.metadata,
            event_sink=request.event_sink,
            confirm=request.confirm,
            created_at=request.created_at,
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
