"""Capability-bound application ports exposed to active plugins."""

from __future__ import annotations

from personal_agent.conversation import ResponseMode, SubmissionOrigin, SubmissionRequest
from personal_agent.delivery import DeliveryKind, DeliveryRequest
from personal_agent.models.messages import OutboundMessage, SessionSource


class PluginConversationPort:
    def __init__(self, *, plugin, coordinator) -> None:
        self._plugin = plugin
        self._coordinator = coordinator

    async def submit(
        self,
        *,
        session_key: str,
        text: str,
        response_mode: ResponseMode | str = ResponseMode.DELIVER,
        metadata: dict | None = None,
    ):
        self._authorize(session_key, capability="active")
        mode = response_mode if isinstance(response_mode, ResponseMode) else ResponseMode(response_mode)
        source = SessionSource(
            platform="plugin",
            user_id=self._plugin.key,
            user_name=self._plugin.manifest.name,
            chat_id=session_key,
        )
        request = SubmissionRequest.text(
            session_key=session_key,
            text=text,
            origin=SubmissionOrigin.PLUGIN,
            response_mode=mode,
            source=source,
            owner_id=self._plugin.key,
            metadata={"plugin_id": self._plugin.key, **dict(metadata or {})},
        )
        return await self._coordinator.submit(request)

    def _authorize(self, session_key: str, *, capability: str) -> None:
        status = str(getattr(self._plugin.status, "value", self._plugin.status) or "")
        if not self._plugin.enabled or status != "loaded":
            raise RuntimeError(f"plugin is not active: {self._plugin.key}")
        if capability not in set(self._plugin.manifest.provides or []):
            raise PermissionError(f"plugin does not declare '{capability}' capability")
        config = dict(getattr(self._plugin.ctx, "config", {}) or {})
        active = config.get("active", {}) if isinstance(config.get("active", {}), dict) else {}
        allowed = active.get("sessions", [])
        allowed = [str(item) for item in allowed] if isinstance(allowed, list) else []
        if "*" not in allowed and session_key not in allowed:
            raise PermissionError(f"plugin cannot access session: {session_key}")


class PluginNotificationPort(PluginConversationPort):
    def __init__(self, *, plugin, coordinator, delivery_service) -> None:
        super().__init__(plugin=plugin, coordinator=coordinator)
        self._delivery_service = delivery_service

    async def send(self, *, session_key: str, text: str, metadata: dict | None = None):
        self._authorize(session_key, capability="notification")
        return await self._delivery_service.deliver(DeliveryRequest(
            session_key=session_key,
            message=OutboundMessage.text(text),
            kind=DeliveryKind.NOTIFICATION,
            metadata={"plugin_id": self._plugin.key, **dict(metadata or {})},
        ))
