"""Memory facade joining stable internal context and dynamic external memory."""

from __future__ import annotations

import logging
from typing import Any

from personal_agent.memory.models import MemoryScope, Observation, ObservationKind

logger = logging.getLogger(__name__)


class MemoryManager:
    def __init__(
        self,
        builtin=None,
        external=None,
        *,
        internal=None,
        router=None,
        archive=None,
        internal_service=None,
    ) -> None:
        self.internal = internal
        self.router = router
        self.archive = archive
        self.internal_service = internal_service
        self._legacy_builtin = builtin
        self._legacy_external = external
        self._last_errors: dict[str, str] = {}

    def get_internal_snapshot(self, session_key: str):
        if self.internal is None:
            raise RuntimeError("Internal memory is unavailable")
        return self.internal.snapshot(session_key=session_key)

    def get_system_prompt_text(self) -> str:
        if self.internal is not None:
            return self.internal.snapshot().content
        return self._legacy_builtin.get_system_prompt_text() if self._legacy_builtin else ""

    def scope(self, *, session_key: str = "", user_id: str = "") -> MemoryScope:
        resolved_user = user_id or _user_id_from_session(session_key)
        profile = self.internal.profile_for_session(session_key) if self.internal is not None else "default"
        return MemoryScope(user_id=resolved_user, session_key=session_key, profile=profile)

    async def prefetch(self, user_message: str, *, session_key: str = "", user_id: str = "") -> list[dict]:
        if self.router is not None:
            try:
                records = await self.router.search(
                    user_message, self.scope(session_key=session_key, user_id=user_id), limit=5
                )
                text = "\n".join(f"- {item.content}" for item in records if item.content)
                return [{"role": "user", "content": [{"type": "text", "text": "相关长期记忆：\n" + text}]}] if text else []
            except Exception as exc:
                self._last_errors["external"] = f"{type(exc).__name__}: {exc}"
                return []
        if self._legacy_external is not None:
            try:
                return await self._legacy_external.prefetch(user_message)
            except Exception as exc:
                self._last_errors["external"] = f"{type(exc).__name__}: {exc}"
        return []

    async def review(self, messages: list[dict[str, Any]], scope: MemoryScope):
        if self.router is None:
            return None
        result = await self.router.review(messages, scope)
        if self.internal_service is not None and result.observations:
            await self.internal_service.enqueue(scope, result.observations, batch_id=result.batch_id)
            if await self.internal_service.should_consolidate(scope):
                await self.internal_service.consolidate(scope)
        await self.router.maybe_recover(scope)
        return result

    async def add_external(self, content: str, *, kind: str = "fact", session_key: str = ""):
        if self.router is None:
            raise RuntimeError("External memory is disabled")
        scope = self.scope(session_key=session_key)
        observation = Observation(kind=ObservationKind(kind), content=content, importance=0.7)
        result = await self.router.migrate((observation,), scope)
        if self.internal_service is not None:
            await self.internal_service.enqueue(scope, (observation,))
        return result

    async def history(self, memory_id: str) -> list[dict[str, Any]]:
        if self.router is None:
            return []
        return [item.as_dict() for item in await self.router.history(memory_id)]

    async def buffer_entries(self, *, status: str = "pending", session_key: str = ""):
        if self.archive is None:
            return []
        return await self.archive.list_buffer(self.scope(session_key=session_key), status=status)

    async def consolidate_internal(self, *, force: bool = True, session_key: str = ""):
        if self.internal_service is None:
            return {"pending": 0, "applied": 0, "skipped": 0, "conflict": 0}
        return await self.internal_service.consolidate(self.scope(session_key=session_key), force=force)

    async def apply_internal(self, observation_id: str, *, session_key: str = "") -> bool:
        return bool(self.internal_service and await self.internal_service.apply_buffer_item(
            self.scope(session_key=session_key), observation_id
        ))

    async def discard_internal(self, observation_id: str, *, session_key: str = "") -> bool:
        if self.archive is None or await self.archive.get_buffer_item(self.scope(session_key=session_key), observation_id) is None:
            return False
        await self.archive.set_buffer_status(observation_id, "skipped", reason="manual discard")
        return True

    async def list_entries(self, *, target: str = "all", session_key: str = "") -> list[dict[str, Any]]:
        if self.router is not None and target in {"all", "external"}:
            records = await self.router.list(self.scope(session_key=session_key))
            return [{**item.as_dict(), "target": "external"} for item in records]
        return await self._legacy_list(target)

    async def search_entries(self, query: str, *, target: str = "all", session_key: str = "") -> list[dict[str, Any]]:
        if self.router is not None and target in {"all", "external"}:
            records = await self.router.search(query, self.scope(session_key=session_key))
            return [{**item.as_dict(), "target": "external"} for item in records]
        if self._legacy_builtin is not None:
            entries = await self._legacy_builtin.search_entries(query, target=target)
            return [{**item, "provider": "builtin"} for item in entries]
        return []

    async def get_entry(self, identifier: str, *, target: str = "all") -> dict[str, Any] | None:
        for entry in await self.list_entries(target=target):
            if str(entry.get("id")) == str(identifier) or str(entry.get("index")) == str(identifier):
                return entry
        return None

    async def delete(self, identifier: str, *, target: str = "all", session_key: str = "") -> bool:
        if self.router is not None and target in {"all", "external"}:
            return await self.router.delete(identifier, self.scope(session_key=session_key))
        if self._legacy_builtin is not None and target in {"all", "memory", "user", "builtin"}:
            return bool(await self._legacy_builtin.delete(identifier, target=target))
        return False

    async def save(self, content: str) -> None:
        if self._legacy_builtin is not None:
            await self._legacy_builtin.save(content)
            if self._legacy_external is not None:
                try:
                    await self._legacy_external.save(content)
                except Exception as exc:
                    self._last_errors["external"] = f"{type(exc).__name__}: {exc}"
            return
        raise RuntimeError("Direct internal memory writes are disabled; use external review or buffer tools")

    async def health_snapshot(self) -> dict[str, Any]:
        if self.router is not None:
            external = self.router.health_snapshot()
            snapshot = self.internal.snapshot() if self.internal is not None else None
            pending = await self.archive.pending_buffer_count(self.scope()) if self.archive else 0
            return {
                "builtin_available": self.internal is not None,
                "builtin_provider": "internal_markdown" if self.internal is not None else "",
                "external_available": external["effective_provider"] != "none",
                "external_provider": external["effective_provider"],
                "requested_provider": external["requested_provider"],
                "effective_provider": external["effective_provider"],
                "fallback_reason": external["fallback_reason"],
                "internal_revision": snapshot.revision if snapshot else 0,
                "internal_profile": snapshot.profile if snapshot else "",
                "buffer_pending": pending,
                "providers": {"internal": {"available": self.internal is not None}, "external": external},
                "last_errors": dict(self._last_errors),
            }
        return await self._legacy_health()

    async def close(self) -> None:
        if self.router is not None:
            await self.router.close()
        if self.archive is not None:
            await self.archive.close()

    @property
    def builtin(self):
        return self.internal or self._legacy_builtin

    @property
    def external(self):
        return self.router or self._legacy_external

    async def _legacy_list(self, target: str) -> list[dict[str, Any]]:
        result = []
        if self._legacy_builtin is not None and target in {"all", "memory", "user", "builtin"}:
            result.extend({**item, "provider": "builtin"} for item in await self._legacy_builtin.list_entries(target=target))
        if self._legacy_external is not None and target in {"all", "external"}:
            try:
                entries = await self._legacy_external.list_entries(target="external")
                result.extend({**item, "provider": "external"} for item in entries)
            except Exception as exc:
                self._last_errors["external"] = f"{type(exc).__name__}: {exc}"
        return result

    async def _legacy_health(self) -> dict[str, Any]:
        builtin = self._legacy_builtin.health_snapshot() if self._legacy_builtin else {"available": False}
        external = self._legacy_external.health_snapshot() if self._legacy_external else {"available": False}
        return {
            "builtin_available": bool(builtin.get("available", True)),
            "builtin_provider": builtin.get("provider", ""),
            "external_available": bool(external.get("available", False)),
            "external_provider": external.get("provider", ""),
            "providers": {"builtin": builtin, "external": external},
            "last_errors": dict(self._last_errors),
        }


def _user_id_from_session(session_key: str) -> str:
    value = str(session_key or "default")
    return value.rsplit(":", 1)[-1] or "default"
