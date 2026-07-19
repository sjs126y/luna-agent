"""Structured plugin lifecycle event journal."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


class PluginEventJournal:
    def __init__(self, store) -> None:
        self._store = store

    def record(
        self,
        plugin_key: str,
        event: str,
        *,
        operation_id: str = "",
        level: str = "info",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        item = {
            "event_id": f"pevt_{uuid4().hex}",
            "plugin_key": str(plugin_key),
            "event": str(event),
            "level": str(level),
            "operation_id": str(operation_id),
            "created_at": datetime.now(UTC).isoformat(),
            "details": dict(details or {}),
        }
        self._store.append_event(str(plugin_key), item)
        return item

    def list(self, plugin_key: str, *, limit: int = 50) -> list[dict[str, Any]]:
        normalized = max(0, int(limit))
        items = self._store.events(plugin_key)
        return list(reversed(items[-normalized:])) if normalized else []

