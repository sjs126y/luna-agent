"""MemoryManager — shell that wires builtin + optional external MemoryProvider."""

from __future__ import annotations

import logging
from typing import Any

from personal_agent.memory.base import MemoryProvider

logger = logging.getLogger(__name__)


class MemoryManager:
    def __init__(
        self,
        builtin: MemoryProvider,
        external: MemoryProvider | None = None,
    ) -> None:
        self._builtin = builtin
        self._external = external
        self._last_errors: dict[str, str] = {}

    # ── system prompt ─────────────────────────────────

    def get_system_prompt_text(self) -> str:
        """Memory text injected into system prompt (builtin only)."""
        return self._builtin.get_system_prompt_text()

    # ── prefetch (external only, injects to api_messages) ──

    async def prefetch(self, user_message: str) -> list[dict]:
        """External memory search results for api_messages injection."""
        if self._external:
            try:
                results = await self._external.prefetch(user_message)
                self._last_errors.pop("external", None)
                return results
            except Exception as exc:
                self._last_errors["external"] = f"{type(exc).__name__}: {exc}"
                logger.debug("External memory prefetch failed, falling back to builtin only: %s", exc)
        return []

    # ── save ──────────────────────────────────────────

    async def save(self, content: str) -> None:
        await self._call_provider("builtin", self._builtin.save(content))
        if self._external:
            try:
                await self._external.save(content)
                self._last_errors.pop("external", None)
            except Exception as exc:
                self._last_errors["external"] = f"{type(exc).__name__}: {exc}"
                logger.warning("External memory save failed, continuing with builtin memory: %s", exc)

    async def list_entries(self, *, target: str = "all") -> list[dict[str, Any]]:
        target = _normalize_target(target)
        entries: list[dict[str, Any]] = []
        if target in {"all", "memory", "user", "builtin"}:
            entries.extend(await self._provider_entries("builtin", self._builtin, target=target))
        if target in {"all", "external"} and self._external is not None:
            entries.extend(await self._provider_entries("external", self._external, target="external"))
        return entries

    async def search_entries(self, query: str, *, target: str = "all") -> list[dict[str, Any]]:
        target = _normalize_target(target)
        entries: list[dict[str, Any]] = []
        if target in {"all", "memory", "user", "builtin"}:
            entries.extend(await self._provider_search("builtin", self._builtin, query, target=target))
        if target in {"all", "external"} and self._external is not None:
            entries.extend(await self._provider_search("external", self._external, query, target="external"))
        return entries

    async def get_entry(self, identifier: str, *, target: str = "all") -> dict[str, Any] | None:
        for entry in await self.list_entries(target=target):
            if _entry_matches(entry, identifier):
                return entry
        return None

    async def delete(self, identifier: str, *, target: str = "all") -> bool:
        target = _normalize_target(target)
        deleted = False
        if target in {"all", "memory", "user", "builtin"}:
            deleted = await self._provider_delete("builtin", self._builtin, identifier, target=target)
            if deleted:
                return True
        if target in {"all", "external"} and self._external is not None:
            return await self._provider_delete("external", self._external, identifier, target="external")
        return False

    async def health_snapshot(self) -> dict[str, Any]:
        builtin = await self._provider_health("builtin", self._builtin)
        external = await self._provider_health("external", self._external)
        return {
            "builtin_available": builtin.get("available", False),
            "builtin_provider": builtin.get("provider", ""),
            "external_available": external.get("available", False),
            "external_provider": external.get("provider", ""),
            "providers": {
                "builtin": builtin,
                "external": external,
            },
            "last_errors": dict(self._last_errors),
        }

    @property
    def builtin(self) -> MemoryProvider:
        return self._builtin

    @property
    def external(self) -> MemoryProvider | None:
        return self._external

    async def _provider_entries(
        self,
        label: str,
        provider: MemoryProvider,
        *,
        target: str,
    ) -> list[dict[str, Any]]:
        try:
            entries = await provider.list_entries(target=target)
            self._last_errors.pop(label, None)
            return [_with_provider(entry, label) for entry in entries]
        except Exception as exc:
            self._last_errors[label] = f"{type(exc).__name__}: {exc}"
            return []

    async def _provider_search(
        self,
        label: str,
        provider: MemoryProvider,
        query: str,
        *,
        target: str,
    ) -> list[dict[str, Any]]:
        try:
            entries = await provider.search_entries(query, target=target)
            self._last_errors.pop(label, None)
            return [_with_provider(entry, label) for entry in entries]
        except Exception as exc:
            self._last_errors[label] = f"{type(exc).__name__}: {exc}"
            return []

    async def _provider_delete(
        self,
        label: str,
        provider: MemoryProvider,
        identifier: str,
        *,
        target: str,
    ) -> bool:
        try:
            deleted = await provider.delete(identifier, target=target)
            self._last_errors.pop(label, None)
            return bool(deleted)
        except Exception as exc:
            self._last_errors[label] = f"{type(exc).__name__}: {exc}"
            return False

    async def _provider_health(
        self,
        label: str,
        provider: MemoryProvider | None,
    ) -> dict[str, Any]:
        if provider is None:
            return {
                "provider": "",
                "available": False,
                "entries": 0,
                "last_error": self._last_errors.get(label, ""),
            }
        try:
            data = dict(provider.health_snapshot())
            data.setdefault("entries", len(await provider.list_entries(target="all")))
            data["available"] = bool(data.get("available", True))
            if not data.get("last_error"):
                data["last_error"] = self._last_errors.get(label, "")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._last_errors[label] = error
            data = {
                "provider": type(provider).__name__,
                "available": False,
                "last_error": error,
            }
        data.setdefault("provider", type(provider).__name__)
        return data

    async def _call_provider(self, label: str, awaitable):
        try:
            value = await awaitable
            self._last_errors.pop(label, None)
            return value
        except Exception as exc:
            self._last_errors[label] = f"{type(exc).__name__}: {exc}"
            raise


def _normalize_target(target: str) -> str:
    value = str(target or "all").strip().lower()
    if value not in {"all", "memory", "user", "builtin", "external"}:
        raise ValueError(f"Invalid memory target: {target}")
    return value


def _with_provider(entry: dict[str, Any], provider: str) -> dict[str, Any]:
    result = dict(entry)
    result.setdefault("provider", provider)
    return result


def _entry_matches(entry: dict[str, Any], identifier: str) -> bool:
    value = str(identifier)
    return value in {
        str(entry.get("id", "")),
        str(entry.get("index", "")),
        f"{entry.get('target', '')}:{entry.get('index', '')}",
    }
