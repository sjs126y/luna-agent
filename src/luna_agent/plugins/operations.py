"""Per-plugin operation serialization and progress tracking."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


class PluginOperationHandle:
    def __init__(self, tracker, item: dict[str, Any]) -> None:
        self._tracker = tracker
        self.item = item

    @property
    def operation_id(self) -> str:
        return str(self.item["operation_id"])

    def stage(self, value: str, *, details: dict[str, Any] | None = None) -> None:
        self.item["stage"] = str(value)
        if details is not None:
            self.item["details"] = dict(details)
        self._tracker._store.put_operation(self.item)


class PluginOperationTracker:
    def __init__(self, store, events) -> None:
        self._store = store
        self._events = events
        self._locks: dict[str, asyncio.Lock] = {}
        self._current: ContextVar[PluginOperationHandle | None] = ContextVar(
            f"plugin-operation:{id(self)}",
            default=None,
        )

    @asynccontextmanager
    async def track(self, plugin_key: str, action: str):
        key = str(plugin_key)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = datetime.now(UTC).isoformat()
            item = {
                "operation_id": f"pop_{uuid4().hex}",
                "plugin_key": key,
                "action": str(action),
                "stage": "started",
                "status": "running",
                "started_at": now,
                "finished_at": "",
                "error": "",
                "details": {},
            }
            handle = PluginOperationHandle(self, item)
            token = self._current.set(handle)
            self._store.put_operation(item)
            self._events.record(key, "operation_started", operation_id=handle.operation_id, details={"action": action})
            try:
                yield handle
            except BaseException as exc:
                item["status"] = "failed"
                item["stage"] = "failed"
                item["finished_at"] = datetime.now(UTC).isoformat()
                item["error"] = f"{type(exc).__name__}: {exc}"
                self._store.put_operation(item)
                self._events.record(
                    key,
                    "operation_failed",
                    operation_id=handle.operation_id,
                    level="error",
                    details={"action": action, "error": item["error"]},
                )
                raise
            else:
                item["status"] = "completed"
                item["stage"] = "completed"
                item["finished_at"] = datetime.now(UTC).isoformat()
                self._store.put_operation(item)
                self._events.record(key, "operation_completed", operation_id=handle.operation_id, details={"action": action})
            finally:
                self._current.reset(token)

    def stage(self, value: str, *, details: dict[str, Any] | None = None) -> None:
        handle = self._current.get()
        if handle is not None:
            handle.stage(value, details=details)

    def current_operation_id(self) -> str:
        handle = self._current.get()
        return handle.operation_id if handle is not None else ""

    def list(self, *, plugin_key: str = "", limit: int = 50) -> list[dict[str, Any]]:
        items = self._store.operations()
        if plugin_key:
            items = [item for item in items if item.get("plugin_key") == plugin_key]
        normalized = max(0, int(limit))
        return list(reversed(items[-normalized:])) if normalized else []

    def get(self, operation_id: str) -> dict[str, Any] | None:
        for item in reversed(self._store.operations()):
            if item.get("operation_id") == operation_id:
                return item
        return None

