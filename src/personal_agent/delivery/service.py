"""Logical message delivery through connected platform adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from personal_agent.delivery.models import (
    DeliveryRequest,
    DeliveryResult,
    DeliveryStatus,
    PlatformSendResult,
)
from personal_agent.hooks import (
    HookEnvelope,
    HookEvent,
    HookScope,
    HookSourceContext,
    PreDeliveryOutcome,
)
from personal_agent.models.messages import OutboundMessage

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
    ) -> None:
        self.sessions = sessions
        self.platforms = platforms
        self.hook_manager = hook_manager
        self.outbox = outbox

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

        try:
            raw = await adapter.send_message(source.chat_id, message)
            sent = PlatformSendResult(
                success=bool(getattr(raw, "success", False)),
                message_id=str(getattr(raw, "message_id", "") or ""),
                error=str(getattr(raw, "error", "") or ""),
                ambiguous="timeout" in str(getattr(raw, "error", "") or "").lower(),
            )
        except Exception as exc:
            sent = PlatformSendResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                ambiguous="timeout" in str(exc).lower(),
            )

        result = DeliveryResult(
            delivery_id=request.delivery_id,
            session_key=request.session_key,
            status=DeliveryStatus.DELIVERED if sent.success else DeliveryStatus.FAILED,
            platform=source.platform,
            chat_id=source.chat_id,
            message_id=sent.message_id,
            error=sent.error,
            attempts=1,
            ambiguous=sent.ambiguous,
        )
        await self._post_delivery(request, source, result)
        return result

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
                "text": message.render_text(),
                "delivery_id": request.delivery_id,
                "kind": request.kind.value,
                "metadata": dict(request.metadata),
            },
        ))
        if isinstance(outcome, PreDeliveryOutcome):
            if outcome.suppressed:
                return None
            if outcome.text is not None:
                return OutboundMessage.text(outcome.text)
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
