"""Logical message delivery through connected platform adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from personal_agent.delivery.models import (
    DeliveryPartResult,
    DeliveryRequest,
    DeliveryResult,
    DeliveryStatus,
    PlatformSendResult,
)
from personal_agent.delivery.planner import DeliveryOperation, DeliveryPlanner
from personal_agent.hooks import (
    HookEnvelope,
    HookEvent,
    HookScope,
    HookSourceContext,
    PreDeliveryOutcome,
)
from personal_agent.models.messages import MessagePart, OutboundMessage

if TYPE_CHECKING:
    from personal_agent.conversation.session_directory import SessionDirectory


class PlatformDirectory:
    def __init__(self) -> None:
        self._adapters: dict[str, Any] = {}

    def register(self, platform: str, adapter: Any) -> None:
        name = str(platform or "").strip()
        if not name:
            raise ValueError("platform is required")
        self._adapters[name] = adapter

    def unregister(self, platform: str) -> None:
        self._adapters.pop(str(platform or "").strip(), None)

    def get(self, platform: str) -> Any | None:
        return self._adapters.get(str(platform or "").strip())

    def snapshot(self) -> dict:
        return {"platforms": sorted(self._adapters)}


class DeliveryService:
    def __init__(
        self,
        *,
        sessions: "SessionDirectory",
        platforms: PlatformDirectory,
        hook_manager=None,
        outbox=None,
        artifact_store=None,
        planner=None,
    ) -> None:
        self.sessions = sessions
        self.platforms = platforms
        self.hook_manager = hook_manager
        self.outbox = outbox
        self.artifact_store = artifact_store
        self.planner = planner or DeliveryPlanner()

    async def deliver(self, request: DeliveryRequest) -> DeliveryResult:
        if self.outbox is not None:
            await self.outbox.enqueue(request)
            if not await self.outbox.claim(request.delivery_id):
                return self._failed(request, "delivery is already being processed", status=DeliveryStatus.DEFERRED)
        result = await self.deliver_once(request)
        if self.outbox is not None:
            return await self.outbox.record_result(result)
        return result

    async def deliver_once(self, request: DeliveryRequest) -> DeliveryResult:
        binding = self.sessions.resolve(request.session_key)
        if binding is None:
            return self._failed(request, "session has no delivery binding")
        source = binding.source
        adapter = self.platforms.get(source.platform)
        if adapter is None:
            return self._failed(
                request,
                f"platform adapter is unavailable: {source.platform}",
                platform=source.platform,
                chat_id=source.chat_id,
                status=DeliveryStatus.DEFERRED,
            )

        message = request.message
        if not request.kind.protected:
            transformed = await self._pre_delivery(request, source, message)
            if transformed is None:
                result = DeliveryResult(
                    delivery_id=request.delivery_id,
                    session_key=request.session_key,
                    status=DeliveryStatus.SUPPRESSED,
                    platform=source.platform,
                    chat_id=source.chat_id,
                )
                await self._post_delivery(request, source, result)
                return result
            message = transformed

        capabilities = getattr(adapter, "capabilities", None)
        from personal_agent.models.messages import PlatformCapabilities

        plan = self.planner.plan(message, capabilities or PlatformCapabilities())
        if self.outbox is not None:
            records = await self.outbox.ensure_parts(request.delivery_id, plan.operations)
        else:
            records = []

        part_results: list[DeliveryPartResult] = []
        for operation in plan.operations:
            record = next(
                (item for item in records if item.operation.index == operation.index),
                None,
            )
            if record is not None and record.status == "delivered":
                part_results.append(DeliveryPartResult(
                    index=operation.index,
                    operation=record.operation,
                    success=True,
                    message_id=record.message_id,
                    attempts=record.attempts,
                    skipped=True,
                ))
                continue
            if record is not None and record.status in {"failed", "ambiguous"}:
                part_results.append(DeliveryPartResult(
                    index=operation.index,
                    operation=record.operation,
                    success=False,
                    message_id=record.message_id,
                    error=record.error,
                    attempts=record.attempts,
                    ambiguous=record.ambiguous,
                    skipped=True,
                ))
                break

            actual = record.operation if record is not None else operation
            if self.outbox is not None:
                await self.outbox.start_part(request.delivery_id, actual.index)
            part = await self._execute_operation(
                request,
                source.chat_id,
                adapter,
                actual,
                attempts=(record.attempts if record else 0) + 1,
            )
            part_results.append(part)
            if self.outbox is not None:
                await self.outbox.record_part_result(request.delivery_id, part)
            if not part.success:
                break

        success = bool(part_results) and len(part_results) == len(plan.operations) and all(
            part.success for part in part_results
        )
        delivered_count = sum(1 for part in part_results if part.success)
        failed_part = next((part for part in part_results if not part.success), None)
        ambiguous = bool(failed_part and failed_part.ambiguous)
        result = DeliveryResult(
            delivery_id=request.delivery_id,
            session_key=request.session_key,
            status=DeliveryStatus.DELIVERED if success else DeliveryStatus.FAILED,
            platform=source.platform,
            chat_id=source.chat_id,
            message_id=next((part.message_id for part in reversed(part_results) if part.message_id), ""),
            error=failed_part.error if failed_part else "",
            attempts=max((part.attempts for part in part_results), default=0),
            ambiguous=ambiguous,
            partial=bool(delivered_count and not success),
            degraded=plan.degraded,
            parts=tuple(part_results),
        )
        await self._post_delivery(request, source, result)
        return result

    async def _execute_operation(
        self,
        request: DeliveryRequest,
        chat_id: str,
        adapter,
        operation: DeliveryOperation,
        *,
        attempts: int,
    ) -> DeliveryPartResult:
        try:
            if operation.kind == "text":
                raw = await adapter.send_message(chat_id, OutboundMessage.text(operation.text))
            else:
                if self.artifact_store is None:
                    raise RuntimeError("artifact store is unavailable")
                ref = await self.artifact_store.get(operation.artifact_id)
                if ref is None or ref.session_key != request.session_key or not ref.delivery_eligible:
                    raise RuntimeError("artifact is unavailable or outside the delivery session")
                path = await self.artifact_store.resolve_path(ref)
                maximum = int(getattr(adapter.capabilities, "max_file_bytes", 0) or 0)
                if maximum and ref.size_bytes > maximum:
                    raise RuntimeError(f"artifact exceeds platform limit of {maximum} bytes")
                raw = await adapter.send_artifact(
                    chat_id,
                    kind=operation.kind,
                    path=path,
                    filename=operation.filename or ref.filename,
                    mime_type=operation.mime_type or ref.mime_type,
                )
            sent = PlatformSendResult(
                success=bool(getattr(raw, "success", False)),
                message_id=str(getattr(raw, "message_id", "") or ""),
                error=str(getattr(raw, "error", "") or ""),
                ambiguous=_is_ambiguous_error(str(getattr(raw, "error", "") or "")),
            )
        except Exception as exc:
            sent = PlatformSendResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                ambiguous=_is_ambiguous_error(str(exc)),
            )
        return DeliveryPartResult(
            index=operation.index,
            operation=operation,
            success=sent.success,
            message_id=sent.message_id,
            error=sent.error,
            attempts=attempts,
            ambiguous=sent.ambiguous,
        )

    async def _pre_delivery(self, request, source, message) -> OutboundMessage | None:
        if self.hook_manager is None:
            return message
        outcome = await self.hook_manager.dispatch(HookEnvelope(
            event_name=HookEvent.PRE_DELIVERY,
            scope=HookScope.SESSION,
            session_key=request.session_key,
            source=HookSourceContext(
                platform=source.platform,
                user_id=source.user_id,
                chat_id=source.chat_id,
            ),
            payload={
                "text": message.text_content(),
                "artifacts": [
                    {
                        "artifact_id": part.artifact_id,
                        "kind": part.type,
                        "filename": part.name,
                        "mime_type": part.mime_type,
                        "size_bytes": int(part.metadata.get("size_bytes") or 0),
                    }
                    for part in message.parts
                    if part.artifact_id
                ],
                "delivery_id": request.delivery_id,
                "kind": request.kind.value,
                "metadata": dict(request.metadata),
            },
        ))
        if isinstance(outcome, PreDeliveryOutcome):
            if outcome.suppressed:
                return None
            removed = set(outcome.removed_artifact_ids)
            parts = [
                part for part in message.parts
                if not (part.artifact_id and part.artifact_id in removed)
                and not (outcome.text is not None and part.type == "text")
            ]
            if outcome.text is not None:
                parts.insert(0, MessagePart(type="text", text=outcome.text))
            if removed or outcome.text is not None:
                return OutboundMessage(parts=parts)
        return message

    async def _post_delivery(self, request, source, result) -> None:
        if self.hook_manager is None:
            return
        await self.hook_manager.dispatch(HookEnvelope(
            event_name=HookEvent.POST_DELIVERY,
            scope=HookScope.SESSION,
            session_key=request.session_key,
            source=HookSourceContext(
                platform=source.platform,
                user_id=source.user_id,
                chat_id=source.chat_id,
            ),
            payload={
                "delivery_id": request.delivery_id,
                "kind": request.kind.value,
                "status": result.status.value,
                "success": result.delivered,
                "error": result.error,
                "partial": result.partial,
                "degraded": result.degraded,
                "parts": [
                    {
                        "index": part.index,
                        "kind": part.operation.kind,
                        "success": part.success,
                        "error": part.error,
                        "ambiguous": part.ambiguous,
                        "attempts": part.attempts,
                    }
                    for part in result.parts
                ],
            },
        ))

    @staticmethod
    def _failed(
        request: DeliveryRequest,
        error: str,
        *,
        platform: str = "",
        chat_id: str = "",
        status: DeliveryStatus = DeliveryStatus.FAILED,
    ) -> DeliveryResult:
        return DeliveryResult(
            delivery_id=request.delivery_id,
            session_key=request.session_key,
            status=status,
            platform=platform,
            chat_id=chat_id,
            error=error,
        )


def _is_ambiguous_error(error: str) -> bool:
    value = str(error or "").lower()
    return "timeout" in value or "timed out" in value or "partial delivery" in value
