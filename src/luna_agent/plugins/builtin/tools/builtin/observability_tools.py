"""Read-only tools for inspecting live domain state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from luna_agent.tools.entry import ToolEntry, ToolHandlerOutput
from luna_agent.tools.registry import tool_registry
from luna_agent.tools.runtime_context import current_tool_agent


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _error(reason: str, message: str) -> ToolHandlerOutput:
    return ToolHandlerOutput(
        text=_json({"ok": False, "reason_code": reason, "error": message}),
        metadata={"reason_code": reason},
        is_error=True,
    )


def _port():
    agent = current_tool_agent()
    port = getattr(agent, "_inspection_port", None) if agent is not None else None
    if port is None:
        raise RuntimeError("Live inspection is unavailable in this runtime")
    return port


async def runtime_inspect() -> str | ToolHandlerOutput:
    try:
        return _json(await _port().runtime_summary())
    except Exception as exc:
        return _error("runtime_inspect_failed", f"{type(exc).__name__}: {exc}")


async def conversation_inspect(
    action: str = "summary",
    session_key: str = "",
    limit: int = 20,
    item_id: int = 0,
) -> str | ToolHandlerOutput:
    try:
        port = _port()
        service = port.conversation()
        action = str(action or "summary").strip().lower()
        bounded = max(1, min(int(limit or 20), 50))
        if action == "summary":
            return _json({
                "ok": True,
                "source": "live",
                "turns": service.turn_report_summary(),
                "tool_runs": await service.tool_run_summary(limit=bounded, session_key=session_key or None),
                "coordinator": port.conversation_runtime_snapshot(),
            })
        if action == "recent_turns":
            return _json({"ok": True, "items": service.recent_turn_reports(bounded)})
        if action == "persisted_turns":
            return _json({"ok": True, "items": await service.recent_persisted_turn_reports(limit=bounded, session_key=session_key or None)})
        if action == "turn":
            if not item_id:
                raise ValueError("item_id is required for turn")
            return _json({"ok": True, "item": await service.persisted_turn_report(int(item_id))})
        if action == "tool_runs":
            return _json({"ok": True, **await service.recent_tool_runs(limit=bounded, session_key=session_key or None)})
        if action == "tool_run":
            if not item_id:
                raise ValueError("item_id is required for tool_run")
            return _json({"ok": True, "item": await service.tool_run_detail(int(item_id))})
        raise ValueError(f"unsupported conversation_inspect action: {action}")
    except Exception as exc:
        return _error("conversation_inspect_failed", f"{type(exc).__name__}: {exc}")


async def platform_inspect(action: str = "list", platform: str = "") -> str | ToolHandlerOutput:
    try:
        gateway = _port().gateway()
        if gateway is None:
            return _json({"ok": False, "reason_code": "gateway_unavailable", "platforms": []})
        health = gateway.health_snapshot()
        platforms = list(health.get("platforms") or [])
        action = str(action or "list").strip().lower()
        if action == "list":
            return _json({"ok": True, "platforms": platforms})
        if action == "info":
            found = next((item for item in platforms if item.get("name") == platform), None)
            return _json({"ok": found is not None, "platform": found or {}, "platform_name": platform})
        raise ValueError(f"unsupported platform_inspect action: {action}")
    except Exception as exc:
        return _error("platform_inspect_failed", f"{type(exc).__name__}: {exc}")


async def config_inspect(action: str = "summary", key: str = "") -> str | ToolHandlerOutput:
    try:
        snapshot = _port().settings().config_snapshot.as_dict()
        action = str(action or "summary").strip().lower()
        if action == "summary":
            return _json({
                "ok": True,
                "field_count": snapshot.get("field_count", 0),
                "source_counts": snapshot.get("source_counts", {}),
                "errors": snapshot.get("errors", []),
                "warnings": snapshot.get("warnings", []),
            })
        if action == "section":
            section = str(key or "").strip()
            return _json({"ok": True, "section": section, "fields": snapshot.get("sections", {}).get(section, [])})
        if action == "field":
            field = next((item for item in snapshot.get("fields", []) if item.get("path") == key), None)
            if key == "auth.owner_ids" and field is not None:
                owner_ids = getattr(_port().settings(), "auth_owner_ids", {}) or {}
                if isinstance(owner_ids, dict):
                    field = dict(field)
                    field["value"] = {
                        "configured": bool(owner_ids),
                        "platforms": sorted(str(platform) for platform in owner_ids),
                        "owner_count": sum(
                            len(values) if isinstance(values, (list, tuple, set, frozenset)) else 0
                            for values in owner_ids.values()
                        ),
                    }
            return _json({"ok": field is not None, "field": field or {}, "key": key})
        raise ValueError(f"unsupported config_inspect action: {action}")
    except Exception as exc:
        return _error("config_inspect_failed", f"{type(exc).__name__}: {exc}")


async def memory_inspect() -> str | ToolHandlerOutput:
    try:
        return _json({"ok": True, "memory": await _port().memory().health_snapshot()})
    except Exception as exc:
        return _error("memory_inspect_failed", f"{type(exc).__name__}: {exc}")


async def audit_inspect(
    event: str = "",
    tool: str = "",
    trace_id: str = "",
    session_key: str = "",
    limit: int = 20,
) -> str | ToolHandlerOutput:
    try:
        from luna_agent.tools.audit import query_audit

        return _json({"ok": True, "items": query_audit(
            event=event, tool=tool, trace_id=trace_id,
            session_key=session_key, limit=max(1, min(int(limit or 20), 100)),
        )})
    except Exception as exc:
        return _error("audit_inspect_failed", f"{type(exc).__name__}: {exc}")


async def logs_query(
    level: str = "",
    logger_name: str = "",
    trace_id: str = "",
    limit: int = 20,
) -> str | ToolHandlerOutput:
    try:
        path = Path(_port().logs_path())
        if not path.exists():
            return _json({"ok": True, "items": [], "available": False, "path": str(path)})
        items: list[dict[str, Any]] = []
        for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if level and str(item.get("level", "")).upper() != str(level).upper():
                continue
            if logger_name and str(item.get("logger", "")) != logger_name:
                continue
            if trace_id and str(item.get("trace_id", "")) != trace_id:
                continue
            items.append(item)
            if len(items) >= max(1, min(int(limit or 20), 100)):
                break
        items.reverse()
        return _json({"ok": True, "available": True, "items": items})
    except Exception as exc:
        return _error("logs_query_failed", f"{type(exc).__name__}: {exc}")


def _entry(name: str, description: str, handler, properties: dict[str, Any]) -> ToolEntry:
    return ToolEntry(
        name=name,
        description=description,
        schema={"type": "object", "properties": properties, "additionalProperties": False},
        handler=handler,
        toolset="builtin",
        permission_category="read",
        tags=["inspect", "diagnostics", name],
        risk_level="low",
        approval_mode="auto",
        idempotent=True,
    )


ENTRIES = (
    _entry("runtime_inspect", "Inspect a shallow live runtime health summary.", runtime_inspect, {}),
    _entry("conversation_inspect", "Inspect conversation state, turn reports, and tool runs.", conversation_inspect, {
        "action": {"type": "string", "enum": ["summary", "recent_turns", "persisted_turns", "turn", "tool_runs", "tool_run"]},
        "session_key": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "item_id": {"type": "integer", "minimum": 1},
    }),
    _entry("platform_inspect", "Inspect platform connection health without credentials.", platform_inspect, {
        "action": {"type": "string", "enum": ["list", "info"]}, "platform": {"type": "string"},
    }),
    _entry("config_inspect", "Inspect effective configuration with sensitive values masked.", config_inspect, {
        "action": {"type": "string", "enum": ["summary", "section", "field"]}, "key": {"type": "string"},
    }),
    _entry("memory_inspect", "Inspect memory provider and maintenance health without memory contents.", memory_inspect, {}),
    _entry("audit_inspect", "Query bounded, redacted audit records.", audit_inspect, {
        "event": {"type": "string"}, "tool": {"type": "string"}, "trace_id": {"type": "string"},
        "session_key": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    }),
    _entry("logs_query", "Query bounded structured runtime logs.", logs_query, {
        "level": {"type": "string"}, "logger_name": {"type": "string"}, "trace_id": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    }),
)


def register(ctx) -> None:
    for entry in ENTRIES:
        ctx.register.tool(entry)


for _entry_item in ENTRIES:
    if tool_registry.get(_entry_item.name) is None:
        tool_registry.register(_entry_item)
