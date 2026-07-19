"""Queueing boundary for all conversation runtime entrypoints."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
from typing import Any, Protocol
import uuid

from luna_agent.commands.policy import CommandExecutionPolicy, command_execution_policy
from luna_agent.commands.runtime import CommandResult
from luna_agent.conversation.input import ConversationInput
from luna_agent.conversation.ledger import (
    DurableSubmissionLedger,
    SubmissionClaimKind,
    SubmissionLedgerRecord,
)
from luna_agent.conversation.steer import ActiveTurnRegistry
from luna_agent.delivery import DeliveryKind, DeliveryRequest, DeliveryStatus
from luna_agent.models.messages import OutboundMessage

from luna_agent.conversation.submission import (
    SubmissionHandle,
    SubmissionKind,
    SubmissionOutcome,
    SubmissionReceipt,
    SubmissionRequest,
    SubmissionStatus,
    ResponseMode,
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
        capability_view=None,
    ): ...


class CoordinatorCommandDispatcher(Protocol):
    async def __call__(self, request: SubmissionRequest) -> CommandResult: ...


@dataclass(slots=True)
class _QueuedSubmission:
    request: SubmissionRequest
    future: asyncio.Future[SubmissionOutcome]


@dataclass(slots=True)
class _SubmissionRecord:
    identity: tuple[str, str, str, str, str]
    future: asyncio.Future[SubmissionOutcome]


class ConversationCoordinator:
    """Serializes turns per session while allowing independent sessions to run."""

    def __init__(
        self,
        conversation_service: ConversationTurnRunner,
        *,
        active_turns: ActiveTurnRegistry | None = None,
        command_dispatcher: CoordinatorCommandDispatcher | None = None,
        delivery_service=None,
        capability_store=None,
        hook_manager=None,
        capability_binder=None,
        submission_ledger: DurableSubmissionLedger | None = None,
        idempotency_cache_size: int = 2048,
    ) -> None:
        self.conversation_service = conversation_service
        self.active_turns = active_turns or ActiveTurnRegistry()
        self.command_dispatcher = command_dispatcher
        self.delivery_service = delivery_service
        self.capability_store = capability_store
        self.hook_manager = hook_manager
        self.capability_binder = capability_binder
        self.submission_ledger = submission_ledger
        self._queues: dict[str, asyncio.Queue[_QueuedSubmission]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._immediate_tasks: set[asyncio.Task[None]] = set()
        self._active: dict[str, SubmissionRequest] = {}
        self._lock = asyncio.Lock()
        self._accepting = True
        self._idempotency_cache_size = max(1, int(idempotency_cache_size))
        self._submissions: OrderedDict[str, _SubmissionRecord] = OrderedDict()

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

            identity = self._submission_identity(request)
            previous = self._submissions.get(request.request_id)
            if previous is not None:
                self._submissions.move_to_end(request.request_id)
                if previous.identity != identity:
                    receipt = SubmissionReceipt(
                        request_id=request.request_id,
                        session_key=request.session_key,
                        accepted=False,
                        status=SubmissionStatus.REJECTED,
                        reason="request_id is already used by another submission",
                    )
                    future.set_result(SubmissionOutcome(
                        request_id=request.request_id,
                        session_key=request.session_key,
                        status=SubmissionStatus.REJECTED,
                        error=receipt.reason,
                    ))
                    return SubmissionHandle(receipt, future)
                receipt = SubmissionReceipt(
                    request_id=request.request_id,
                    session_key=request.session_key,
                    accepted=True,
                    status=(
                        previous.future.result().status
                        if previous.future.done() and not previous.future.cancelled()
                        else SubmissionStatus.ACCEPTED
                    ),
                    queue_position=0,
                    reason="duplicate request_id reused existing submission",
                )
                return SubmissionHandle(receipt, previous.future)

            if request.durable:
                if self.submission_ledger is None:
                    return self._rejected_handle(
                        request,
                        future,
                        "durable submission ledger is unavailable",
                    )
                claim = await self.submission_ledger.claim(request)
                if claim.kind == SubmissionClaimKind.CONFLICT:
                    return self._rejected_handle(
                        request,
                        future,
                        "request_id is already used by another durable submission payload",
                    )
                if claim.kind == SubmissionClaimKind.CACHED:
                    future.set_result(claim.record.outcome())
                    self._submissions[request.request_id] = _SubmissionRecord(identity, future)
                    self._prune_submissions()
                    receipt = SubmissionReceipt(
                        request_id=request.request_id,
                        session_key=request.session_key,
                        accepted=True,
                        status=claim.record.outcome().status,
                        reason="durable duplicate reused persisted submission",
                    )
                    return SubmissionHandle(receipt, future)
                if claim.kind == SubmissionClaimKind.ACTIVE:
                    future.set_result(claim.record.outcome())
                    receipt = SubmissionReceipt(
                        request_id=request.request_id,
                        session_key=request.session_key,
                        accepted=True,
                        status=SubmissionStatus.ACCEPTED,
                        reason="durable submission is already active",
                    )
                    return SubmissionHandle(receipt, future)
                if claim.kind == SubmissionClaimKind.RESUME:
                    self._submissions[request.request_id] = _SubmissionRecord(identity, future)
                    task = asyncio.create_task(
                        self._resume_durable(request, claim.record, future),
                        name=f"submission-resume:{request.request_id}",
                    )
                    self._immediate_tasks.add(task)
                    task.add_done_callback(self._immediate_tasks.discard)
                    receipt = SubmissionReceipt(
                        request_id=request.request_id,
                        session_key=request.session_key,
                        accepted=True,
                        status=SubmissionStatus.ACCEPTED,
                        reason="resuming durable submission delivery",
                    )
                    return SubmissionHandle(receipt, future)

            self._submissions[request.request_id] = _SubmissionRecord(identity, future)
            self._prune_submissions()

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

    @staticmethod
    def _submission_identity(request: SubmissionRequest) -> tuple[str, str, str, str, str]:
        attachment_ids = "\0".join(
            str(getattr(item, "id", "") or "")
            for item in request.input.attachments
        )
        content_hash = hashlib.sha256(
            f"{request.input.text}\0{attachment_ids}".encode("utf-8")
        ).hexdigest()
        return (
            request.session_key,
            request.origin.value,
            request.owner_id,
            request.response_mode.value,
            content_hash,
        )

    def _prune_submissions(self) -> None:
        if len(self._submissions) <= self._idempotency_cache_size:
            return
        for request_id, record in list(self._submissions.items()):
            if len(self._submissions) <= self._idempotency_cache_size:
                break
            if record.future.done():
                self._submissions.pop(request_id, None)

    @staticmethod
    def _rejected_handle(
        request: SubmissionRequest,
        future: asyncio.Future[SubmissionOutcome],
        reason: str,
    ) -> SubmissionHandle:
        receipt = SubmissionReceipt(
            request_id=request.request_id,
            session_key=request.session_key,
            accepted=False,
            status=SubmissionStatus.REJECTED,
            reason=reason,
        )
        future.set_result(SubmissionOutcome(
            request_id=request.request_id,
            session_key=request.session_key,
            status=SubmissionStatus.REJECTED,
            error=reason,
        ))
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
                    try:
                        outcome = await self._execute(item.request)
                        await self._persist_conversation(item.request, outcome)
                        outcome = await self._apply_response_mode(item.request, outcome)
                    except Exception as exc:
                        outcome = SubmissionOutcome(
                            request_id=item.request.request_id,
                            session_key=item.request.session_key,
                            status=SubmissionStatus.FAILED,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    outcome = await self._safe_persist_final(item.request, outcome)
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
        if self.capability_store is not None:
            async with await self.capability_store.acquire() as lease:
                view = lease.view()
                if self.capability_binder is not None:
                    with self.capability_binder(view):
                        return await self._execute_with_capabilities(request, view)
                if self.hook_manager is not None:
                    from luna_agent.plugins.runtime import CapabilityKind

                    hook_ids = {
                        route.manager_key
                        for routes in view.routes.get(CapabilityKind.HOOK, {}).values()
                        for route in routes
                    }
                    with self.hook_manager.bind_routes(hook_ids):
                        return await self._execute_with_capabilities(request, view)
                return await self._execute_with_capabilities(request, view)
        return await self._execute_with_capabilities(request, None)

    async def _execute_with_capabilities(
        self,
        request: SubmissionRequest,
        capability_view,
    ) -> SubmissionOutcome:
        started_at = datetime.now(UTC)
        if request.durable and self.submission_ledger is not None:
            await self.submission_ledger.mark_running(request)
        command_result = await self._dispatch_command(request)
        if command_result is not None and command_result.continue_text is None:
            return self._command_outcome(request, command_result, started_at=started_at)
        if command_result is not None and command_result.continue_text is not None:
            request = self._with_text(request, command_result.continue_text)
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        if request.durable and self.submission_ledger is not None:
            await self.submission_ledger.mark_running(request, turn_id=turn_id)
        capture_policy = getattr(self.conversation_service, "capture_turn_policy", None)
        policy_snapshot = capture_policy(request.session_key) if capture_policy else None
        self.active_turns.begin_turn(
            request.session_key,
            turn_id,
            request_id=request.request_id,
            task=asyncio.current_task(),
        )
        try:
            kwargs = {
                "event_sink": request.event_sink,
                "confirm": request.confirm,
                "turn_id": turn_id,
                "steer": self.active_turns,
                "policy_snapshot": policy_snapshot,
            }
            if capability_view is not None:
                kwargs["capability_view"] = capability_view
            result = await self.conversation_service.run_turn_input_events(
                request.session_key,
                request.input,
                **kwargs,
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
        structured_message = getattr(result, "outbound_message", None)
        if structured_message is not None and not getattr(structured_message, "parts", None):
            structured_message = None
        return SubmissionOutcome(
            request_id=request.request_id,
            session_key=request.session_key,
            status=status,
            response=str(getattr(result, "final_response", "") or ""),
            error=str(getattr(result, "error", "") or ""),
            payload={"turn_result": result},
            message=structured_message,
            started_at=started_at,
        )

    async def _complete_immediate(
        self,
        request: SubmissionRequest,
        future: asyncio.Future[SubmissionOutcome],
        policy: CommandExecutionPolicy,
    ) -> None:
        if self.capability_store is not None:
            async with await self.capability_store.acquire() as lease:
                view = lease.view()
                if self.capability_binder is not None:
                    with self.capability_binder(view):
                        await self._complete_immediate_bound(request, future, policy)
                        return
                await self._complete_immediate_bound(request, future, policy)
                return
        await self._complete_immediate_bound(request, future, policy)

    async def _complete_immediate_bound(
        self,
        request: SubmissionRequest,
        future: asyncio.Future[SubmissionOutcome],
        policy: CommandExecutionPolicy,
    ) -> None:
        started_at = datetime.now(UTC)
        try:
            if request.durable and self.submission_ledger is not None:
                await self.submission_ledger.mark_running(request)
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
            await self._persist_conversation(request, outcome)
            outcome = await self._apply_response_mode(request, outcome)
        except Exception as exc:
            outcome = SubmissionOutcome(
                request_id=request.request_id,
                session_key=request.session_key,
                status=SubmissionStatus.FAILED,
                kind=SubmissionKind.COMMAND,
                error=f"{type(exc).__name__}: {exc}",
                started_at=started_at,
            )
        outcome = await self._safe_persist_final(request, outcome)
        if not future.done():
            future.set_result(outcome)

    async def _resume_durable(
        self,
        request: SubmissionRequest,
        record: SubmissionLedgerRecord,
        future: asyncio.Future[SubmissionOutcome],
    ) -> None:
        try:
            outcome = await self._apply_response_mode(request, record.outcome())
        except Exception as exc:
            outcome = SubmissionOutcome(
                request_id=request.request_id,
                session_key=request.session_key,
                status=SubmissionStatus.FAILED,
                kind=record.kind,
                response=record.response,
                message=record.message,
                error=f"{type(exc).__name__}: {exc}",
            )
        outcome = await self._safe_persist_final(request, outcome)
        if not future.done():
            future.set_result(outcome)

    async def _persist_conversation(
        self,
        request: SubmissionRequest,
        outcome: SubmissionOutcome,
    ) -> None:
        if not request.durable or self.submission_ledger is None:
            return
        if (
            request.response_mode == ResponseMode.DELIVER
            and outcome.status == SubmissionStatus.COMPLETED
            and (outcome.response or outcome.message)
        ):
            await self.submission_ledger.store_conversation(request, outcome)

    async def _persist_final(
        self,
        request: SubmissionRequest,
        outcome: SubmissionOutcome,
    ) -> None:
        if not request.durable or self.submission_ledger is None:
            return
        delivery = outcome.payload.get("delivery_result") if outcome.payload else None
        delivery_id = str(getattr(delivery, "delivery_id", "") or "")
        pending = getattr(delivery, "status", None) == DeliveryStatus.DEFERRED
        await self.submission_ledger.store_final(
            request,
            outcome,
            delivery_id=delivery_id,
            delivery_pending=pending,
        )

    async def _safe_persist_final(
        self,
        request: SubmissionRequest,
        outcome: SubmissionOutcome,
    ) -> SubmissionOutcome:
        try:
            await self._persist_final(request, outcome)
            return outcome
        except Exception as exc:
            return SubmissionOutcome(
                request_id=outcome.request_id,
                session_key=outcome.session_key,
                status=SubmissionStatus.FAILED,
                kind=outcome.kind,
                response=outcome.response,
                error=f"durable submission persistence failed: {type(exc).__name__}: {exc}",
                payload=dict(outcome.payload),
                message=outcome.message,
                started_at=outcome.started_at,
            )

    async def _apply_response_mode(
        self,
        request: SubmissionRequest,
        outcome: SubmissionOutcome,
    ) -> SubmissionOutcome:
        if request.response_mode != ResponseMode.DELIVER or not (outcome.response or outcome.message):
            return outcome
        if self.delivery_service is None:
            return SubmissionOutcome(
                request_id=outcome.request_id,
                session_key=outcome.session_key,
                status=SubmissionStatus.FAILED,
                kind=outcome.kind,
                response=outcome.response,
                error="delivery service is unavailable",
                payload=dict(outcome.payload),
                started_at=outcome.started_at,
            )
        kind = DeliveryKind.COMMAND if outcome.kind == SubmissionKind.COMMAND else DeliveryKind.CONVERSATION
        delivery = await self.delivery_service.deliver(DeliveryRequest(
            session_key=request.session_key,
            message=(
                outcome.message
                if outcome.message is not None and outcome.message.parts
                else OutboundMessage.text(outcome.response)
            ),
            kind=kind,
            delivery_id=(
                self.submission_ledger.delivery_id(request)
                if request.durable and self.submission_ledger is not None
                else f"del_{uuid.uuid4().hex}"
            ),
            metadata={"submission_id": request.request_id},
        ))
        payload = dict(outcome.payload)
        payload["delivery_result"] = delivery
        delivery_failed = delivery.status not in {
            DeliveryStatus.DELIVERED,
            DeliveryStatus.DEFERRED,
            DeliveryStatus.SUPPRESSED,
        }
        return SubmissionOutcome(
            request_id=outcome.request_id,
            session_key=outcome.session_key,
            status=SubmissionStatus.FAILED if delivery_failed else outcome.status,
            kind=outcome.kind,
            response=outcome.response,
            error=outcome.error or (delivery.error if delivery_failed else ""),
            payload=payload,
            message=outcome.message,
            started_at=outcome.started_at,
        )

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
            durable=request.durable,
            metadata=request.metadata,
            event_sink=request.event_sink,
            confirm=request.confirm,
            command_runtime=request.command_runtime,
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
