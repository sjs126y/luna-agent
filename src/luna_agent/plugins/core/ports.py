"""Capability-bound application ports exposed to active plugins."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from luna_agent.conversation import ResponseMode, SubmissionOrigin, SubmissionRequest
from luna_agent.conversation.input import ConversationInput
from luna_agent.delivery import DeliveryKind, DeliveryRequest
from luna_agent.models.messages import AttachmentRef, OutboundMessage, SessionSource
from luna_agent.persistence.json_store import write_json_atomic
from luna_agent.plugins.runtime import PluginRuntimeState
from luna_agent.plugins.active.contracts import ActiveConversationIntent


class PluginConversationPort:
    def __init__(self, *, plugin, coordinator, artifact_store=None) -> None:
        self._plugin = plugin
        self._coordinator = coordinator
        self._artifact_store = artifact_store

    async def submit(
        self,
        *,
        session_key: str,
        text: str,
        response_mode: ResponseMode | str = ResponseMode.DELIVER,
        metadata: dict | None = None,
        request_id: str = "",
        durable: bool | None = None,
        artifact_ids: list[str] | tuple[str, ...] = (),
    ):
        self._authorize(session_key, capability="active")
        mode = response_mode if isinstance(response_mode, ResponseMode) else ResponseMode(response_mode)
        source = SessionSource(
            platform="plugin",
            user_id=self._plugin.key,
            user_name=self._plugin.manifest.name,
            chat_id=session_key,
        )
        attachments = await self._artifact_attachments(session_key, artifact_ids)
        explicit_request_id = str(request_id or "").strip()
        durable_submission = bool(explicit_request_id) if durable is None else bool(durable)
        if durable_submission and not explicit_request_id:
            raise ValueError("durable plugin submissions require a stable request_id")
        request = SubmissionRequest(
            session_key=session_key,
            input=ConversationInput(
                text=str(text),
                source=source,
                attachments=attachments,
                metadata={"plugin_id": self._plugin.key},
            ),
            origin=SubmissionOrigin.PLUGIN,
            response_mode=mode,
            request_id=explicit_request_id or f"plugin:{self._plugin.runtime_instance_id}:{uuid4().hex}",
            owner_id=self._plugin.key,
            durable=durable_submission,
            metadata={"plugin_id": self._plugin.key, **dict(metadata or {})},
        )
        return await self._coordinator.submit(request)

    async def submit_intent(self, intent: ActiveConversationIntent):
        self._authorize(intent.session_key, capability="active")
        if not isinstance(intent, ActiveConversationIntent):
            raise TypeError("active conversation intent has an invalid type")
        request_id = str(intent.request_id or f"intent:{intent.intent_id}").strip()
        request = SubmissionRequest(
            session_key=intent.session_key,
            input=ConversationInput(
                source=SessionSource(
                    platform="plugin",
                    user_id=self._plugin.key,
                    user_name=self._plugin.manifest.name,
                    chat_id=intent.session_key,
                ),
                active_intent=intent,
                metadata={"plugin_id": self._plugin.key, "active_intent": intent.intent_id},
            ),
            origin=SubmissionOrigin.ACTIVE_PLUGIN,
            response_mode=ResponseMode.DELIVER,
            request_id=request_id,
            owner_id=self._plugin.key,
            durable=True,
            metadata={"plugin_id": self._plugin.key, "active_intent": intent.intent_id},
        )
        return await self._coordinator.submit(request)

    async def status(self, session_key: str):
        self._authorize(session_key, capability="active")
        status = getattr(self._coordinator, "status", None)
        if status is None:
            raise RuntimeError("conversation status is unavailable")
        return await status(session_key)

    async def _artifact_attachments(
        self,
        session_key: str,
        artifact_ids: list[str] | tuple[str, ...],
    ) -> list[AttachmentRef]:
        requested = tuple(dict.fromkeys(str(value or "").strip() for value in artifact_ids))
        if not requested:
            return []
        if self._artifact_store is None:
            raise RuntimeError("plugin artifact submission is unavailable")
        attachments: list[AttachmentRef] = []
        for artifact_id in requested:
            if not artifact_id:
                continue
            ref = await self._artifact_store.get(artifact_id)
            if ref is None:
                raise KeyError(f"plugin artifact not found: {artifact_id}")
            if ref.owner_id != self._plugin.key:
                raise PermissionError("plugin cannot submit an artifact owned by another runtime")
            if ref.session_key != session_key:
                raise PermissionError("plugin artifact belongs to another session")
            path = await self._artifact_store.resolve_path(ref)
            attachments.append(AttachmentRef(
                id=ref.artifact_id,
                kind=ref.kind,
                name=ref.filename,
                mime_type=ref.mime_type,
                size=ref.size_bytes,
                local_path=str(path),
                metadata={"artifact_id": ref.artifact_id, "source": "plugin"},
            ))
        return attachments

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
        self._validate()
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

    def exists(self, relative_path: str | Path) -> bool:
        return self.resolve(relative_path).exists()

    def read_json(
        self,
        relative_path: str | Path,
        *,
        default: Any = None,
        schema_version: int | None = None,
    ) -> Any:
        path = self.resolve(relative_path)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        if schema_version is not None:
            if not isinstance(value, dict) or int(value.get("schema_version") or 0) != schema_version:
                raise ValueError(f"plugin storage schema mismatch: {relative_path}")
        return value

    def write_json_atomic(self, relative_path: str | Path, value: Any) -> Path:
        path = self.resolve(relative_path)
        write_json_atomic(path, value)
        return path

    def _validate(self) -> None:
        scope = getattr(self._plugin, "generation_scope", None)
        if scope is not None and scope.closed:
            raise RuntimeError(f"plugin generation is no longer active: {self._plugin.key}")


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
            from luna_agent.llm.provider import provider_registry
            from luna_agent.llm.transport_registry import transport_registry

            provider_name = self._settings.llm_provider
            self._provider = provider_registry.get(provider_name, self._settings)
            self._transport = transport_registry.get(self._provider.api_mode, self._provider)
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
