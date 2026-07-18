"""Persistent bounded state for plugin operations and lifecycle events."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Any

from personal_agent.persistence.json_store import read_json_object, write_json_atomic
from personal_agent.text_safety import clean_payload

_MAX_OPERATIONS = 200
_MAX_EVENTS_PER_PLUGIN = 100


class PluginControlStateStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = RLock()
        self._state = self._load()
        self._interrupt_unfinished()

    def operations(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._state["operations"])

    def events(self, plugin_key: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._state["events"].get(plugin_key, []))

    def put_operation(self, operation: dict[str, Any]) -> None:
        item = clean_payload(dict(operation))
        operation_id = str(item.get("operation_id") or "")
        with self._lock:
            items = self._state["operations"]
            for index, current in enumerate(items):
                if str(current.get("operation_id") or "") == operation_id:
                    items[index] = item
                    break
            else:
                items.append(item)
            self._state["operations"] = items[-_MAX_OPERATIONS:]
            self._save()

    def append_event(self, plugin_key: str, event: dict[str, Any]) -> None:
        item = clean_payload(dict(event))
        with self._lock:
            items = self._state["events"].setdefault(plugin_key, [])
            items.append(item)
            self._state["events"][plugin_key] = items[-_MAX_EVENTS_PER_PLUGIN:]
            self._save()

    def _load(self) -> dict[str, Any]:
        data = read_json_object(
            self.path,
            {"schema_version": 1, "revision": 0, "operations": [], "events": {}},
        )
        if int(data.get("schema_version") or 0) != 1:
            raise ValueError("Unsupported plugin control state schema")
        operations = data.get("operations") if isinstance(data.get("operations"), list) else []
        events = data.get("events") if isinstance(data.get("events"), dict) else {}
        return {
            "schema_version": 1,
            "revision": int(data.get("revision") or 0),
            "operations": list(operations)[-_MAX_OPERATIONS:],
            "events": {
                str(key): list(items)[-_MAX_EVENTS_PER_PLUGIN:]
                for key, items in events.items()
                if isinstance(items, list)
            },
        }

    def _interrupt_unfinished(self) -> None:
        changed = False
        for item in self._state["operations"]:
            if item.get("status") == "running":
                item["status"] = "interrupted"
                item["stage"] = "interrupted"
                item["error"] = "process stopped before operation completed"
                changed = True
        if changed:
            self._save()

    def _save(self) -> None:
        self._state["revision"] = int(self._state.get("revision") or 0) + 1
        write_json_atomic(self.path, self._state)

