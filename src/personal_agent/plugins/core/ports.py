"""Capability-bound application ports exposed to active plugins."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from personal_agent.conversation import ResponseMode, SubmissionOrigin, SubmissionRequest
from personal_agent.delivery import DeliveryKind, DeliveryRequest
from personal_agent.models.messages import OutboundMessage, SessionSource
from personal_agent.plugins.runtime import PluginRuntimeState


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
        if not self._plugin.enabled or self._plugin.runtime_state is not PluginRuntimeState.ACTIVE:
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
    def __init__(
        self,
        *,
        plugin,
        coordinator,
        delivery_service,
        capability: str = "notification",
    ) -> None:
        super().__init__(plugin=plugin, coordinator=coordinator)
        self._delivery_service = delivery_service
        self._capability = capability

    async def send(self, *, session_key: str, text: str, metadata: dict | None = None):
        self._authorize(session_key, capability=self._capability)
        return await self._delivery_service.deliver(DeliveryRequest(
            session_key=session_key,
            message=OutboundMessage.text(text),
            kind=DeliveryKind.NOTIFICATION,
            metadata={"plugin_id": self._plugin.key, **dict(metadata or {})},
        ))


class PluginStoragePort:
    def __init__(self, *, plugin, root: Path) -> None:
        self._plugin = plugin
        self.root = (
            Path(plugin.data_path)
            if getattr(plugin, "data_path", None) is not None
            else Path(root) / plugin.key.replace("/", "__")
        )
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative_path: str | Path) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise ValueError("plugin storage path must be relative")
        resolved_root = self.root.resolve()
        resolved = (resolved_root / candidate).resolve()
        if resolved != resolved_root and resolved_root not in resolved.parents:
            raise ValueError("plugin storage path escapes isolated root")
        return resolved

    def read_text(self, relative_path: str | Path, *, default: str = "") -> str:
        path = self.resolve(relative_path)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return default

    def write_text(self, relative_path: str | Path, text: str) -> Path:
        path = self.resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(text), encoding="utf-8")
        return path


class PluginTaskPort:
    def __init__(self, *, plugin, tasks: dict[str, set[asyncio.Task]]) -> None:
        self._plugin = plugin
        self._tasks = tasks

    def create(self, awaitable, *, name: str = "") -> asyncio.Task:
        if self._plugin.runtime_state is not PluginRuntimeState.ACTIVE:
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise RuntimeError(f"plugin is not active: {self._plugin.key}")
        runtime_id = self._plugin.runtime_instance_id
        task = asyncio.create_task(
            awaitable,
            name=name or f"plugin:{self._plugin.key}:{runtime_id}",
        )
        bucket = self._tasks.setdefault(runtime_id, set())
        bucket.add(task)
        task.add_done_callback(bucket.discard)
        return task


class PluginLLMPort:
    def __init__(self, *, plugin, settings) -> None:
        self._plugin = plugin
        self._settings = settings
        self._provider = None
        self._transport = None

    async def complete(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        messages: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ):
        self._validate()
        if self._transport is None:
            from personal_agent.llm.provider import provider_registry
            from personal_agent.llm.transport_registry import transport_registry

            provider_name = self._settings.llm_provider
            self._provider = provider_registry.get(provider_name, self._settings)
            api_mode = str(getattr(self._settings, "llm_api_mode", "auto") or "auto")
            if api_mode == "auto":
                api_mode = provider_registry.detect_api_mode(
                    self._settings.llm_base_url,
                    provider_name,
                )
            self._transport = transport_registry.get(api_mode, self._provider)
            self._plugin.generation_scope.defer("llm-transport", self._transport.close)
        request_messages = list(messages or [])
        request_messages.append({"role": "user", "content": str(prompt)})
        return await self._transport.call(
            messages=request_messages,
            system_prompt=str(system_prompt),
            tools=[],
            max_tokens=int(max_tokens or self._provider.max_tokens),
            stream=False,
        )

    def _validate(self) -> None:
        scope = self._plugin.generation_scope
        if scope is None or scope.closed:
            raise RuntimeError(f"plugin generation is no longer active: {self._plugin.key}")


class PluginArtifactPort:
    def __init__(self, *, plugin, store) -> None:
        self._plugin = plugin
        self._store = store

    async def create(
        self,
        data: bytes,
        *,
        kind: str,
        filename: str,
        mime_type: str,
        session_key: str,
        turn_id: str,
        metadata: dict[str, Any] | None = None,
    ):
        self._validate()
        return await self._store.create(
            data,
            kind=kind,
            filename=filename,
            mime_type=mime_type,
            session_key=session_key,
            turn_id=turn_id,
            source="plugin",
            source_name=self._plugin.key,
            owner_id=self._plugin.key,
            metadata=dict(metadata or {}),
        )

    async def get(self, artifact_id: str):
        self._validate()
        ref = await self._store.get(artifact_id)
        if ref is not None and ref.owner_id != self._plugin.key:
            raise PermissionError("plugin cannot access an artifact owned by another runtime")
        return ref

    def _validate(self) -> None:
        scope = self._plugin.generation_scope
        if scope is None or scope.closed:
            raise RuntimeError(f"plugin generation is no longer active: {self._plugin.key}")
