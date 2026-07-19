"""Unified runtime activity snapshots for UI and command surfaces."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any


PUBLIC_STATUSES = {"running", "completed", "failed", "stopped", "stopping"}


def activity_snapshot(
    *,
    gateway_snapshot: dict[str, Any] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Return a stable snapshot of sub-agent, process, and gateway activity."""
    limit = max(1, int(limit or 20))
    sub_agents = sub_agent_snapshot(limit=limit)
    background_processes = background_process_snapshot(limit=limit)
    gateway_agents = gateway_agent_snapshot(gateway_snapshot=gateway_snapshot, limit=limit)

    active_sub_agents = sub_agents["counts"]["active"]
    running_processes = background_processes["counts"]["running"]
    running_gateway = gateway_agents["counts"]["running"]
    active_total = active_sub_agents + running_processes + running_gateway

    all_items = [
        *sub_agents["active_runs"],
        *background_processes["items"],
        *gateway_agents["running_agent_runs"],
    ]
    longest = max(
        [float(item.get("duration_seconds") or 0.0) for item in all_items],
        default=0.0,
    )
    gateway_longest = float((gateway_snapshot or {}).get("longest_running_seconds") or 0.0)
    longest = max(longest, gateway_longest)

    return {
        "summary": {
            "has_active_work": active_total > 0,
            "active_total": active_total,
            "attention_required": any(bool(item.get("attention_required")) for item in all_items)
            or sub_agents["counts"]["failed_recent"] > 0,
            "longest_running_seconds": round(longest, 3),
            "counts": {
                "sub_agents": dict(sub_agents["counts"]),
                "background_processes": dict(background_processes["counts"]),
                "gateway_agents": dict(gateway_agents["counts"]),
            },
        },
        "sub_agents": sub_agents,
        "background_processes": background_processes,
        "gateway_agents": gateway_agents,
    }


def activity_detail(
    kind: str,
    id_: str,
    *,
    gateway_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return one detailed activity item by public kind and id."""
    normalized_kind = _normalize_kind(kind)
    if normalized_kind == "sub_agent":
        run = _sub_agent_detail(id_)
        if run is None:
            return None
        return {"kind": "sub_agent", "id": str(id_), "run": run}
    if normalized_kind == "background_process":
        process = _background_process_detail(id_)
        if process is None:
            return None
        return {"kind": "background_process", "id": str(id_), "process": process}
    if normalized_kind == "gateway_agent":
        run = _gateway_agent_detail(id_, gateway_snapshot=gateway_snapshot)
        if run is None:
            return None
        return {"kind": "gateway_agent", "id": str(id_), "gateway_run": run}
    return None


def activity_choices(
    provider: str,
    *,
    query: str = "",
    limit: int = 20,
    gateway_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return dynamic slash-command candidates for activity detail ids."""
    limit = max(1, int(limit or 20))
    if provider in {"activity_agents", "agents", "sub_agents"}:
        return _sub_agent_choices(query=query, limit=limit)
    if provider in {"activity_processes", "processes", "background_processes"}:
        from luna_agent.plugins.builtin.tools.builtin import process_tool

        return process_tool.process_choices(query=query, limit=limit)
    if provider in {"activity_gateway", "gateway", "gateway_agents"}:
        return _gateway_agent_choices(
            query=query,
            limit=limit,
            gateway_snapshot=gateway_snapshot,
        )
    return []


def sub_agent_snapshot(*, limit: int = 20) -> dict[str, Any]:
    from luna_agent.plugins.builtin.tools.builtin import delegate

    active = [_sub_agent_item(item, active=True) for item in delegate.list_active_agent_runs()]
    recent = [_sub_agent_item(item, active=False) for item in delegate.list_agent_runs(limit=limit)]
    failed_recent = sum(1 for item in recent if item.get("status") == "failed")
    stop_requested = sum(
        1
        for item in [*active, *recent]
        if bool(item.get("stop_requested")) or item.get("status") == "stopping"
    )
    return {
        "counts": {
            "active": len(active),
            "recent": len(recent),
            "failed_recent": failed_recent,
            "stop_requested": stop_requested,
        },
        "active_runs": active,
        "recent_runs": recent,
    }


def background_process_snapshot(*, limit: int = 20) -> dict[str, Any]:
    from luna_agent.plugins.builtin.tools.builtin import process_tool

    snapshot = process_tool.process_snapshot(limit=limit)
    items = list(snapshot.get("items", []))
    return {
        "counts": {
            "total": int(snapshot.get("total", 0)),
            "running": int(snapshot.get("running", 0)),
            "done": int(snapshot.get("done", 0)),
            "killed": int(snapshot.get("killed", 0)),
        },
        "items": items,
    }


def gateway_agent_snapshot(
    *,
    gateway_snapshot: dict[str, Any] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    gateway_snapshot = gateway_snapshot or {}
    raw_runs = list(gateway_snapshot.get("running_agent_runs") or [])[:limit]
    runs = [_gateway_agent_item(item) for item in raw_runs if isinstance(item, dict)]
    return {
        "counts": {
            "running": int(gateway_snapshot.get("running_agents") or len(runs)),
            "stop_requested": int(
                gateway_snapshot.get("stop_requested_agents")
                or sum(1 for item in runs if item.get("stop_requested"))
            ),
        },
        "running_agent_runs": runs,
    }


def _sub_agent_detail(run_id: str) -> dict[str, Any] | None:
    from luna_agent.plugins.builtin.tools.builtin import delegate

    run = delegate.get_agent_run(run_id)
    if run is not None:
        data = asdict(run) if is_dataclass(run) else dict(run)
        return {
            **_sub_agent_item(data, active=False),
            "raw_status": str(data.get("status", "")),
            "messages": list(data.get("messages") or []),
            "tool_calls": list(data.get("tool_calls") or []),
            "executed_tool_call_details": list(data.get("executed_tool_calls") or []),
            "denied_tool_call_details": list(data.get("denied_tool_calls") or []),
            "tool_result_summaries": list(data.get("tool_results") or []),
            "denied_tool_selection_details": list(data.get("denied_tools") or []),
            "result": str(data.get("result") or ""),
        }

    for item in delegate.list_active_agent_runs():
        if str(item.get("run_id") or item.get("id") or "") == str(run_id):
            return _sub_agent_item(item, active=True)
    return None


def _background_process_detail(pid: str) -> dict[str, Any] | None:
    from luna_agent.plugins.builtin.tools.builtin import process_tool

    try:
        return process_tool.process_detail(int(pid))
    except (TypeError, ValueError):
        return None


def _gateway_agent_detail(
    session_key: str,
    *,
    gateway_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    for item in (gateway_snapshot or {}).get("running_agent_runs") or []:
        if isinstance(item, dict) and str(item.get("session_key", "")) == str(session_key):
            return _gateway_agent_item(item)
    return None


def _sub_agent_choices(*, query: str, limit: int) -> list[dict[str, Any]]:
    snapshot = sub_agent_snapshot(limit=limit)
    candidates = [*snapshot["active_runs"], *snapshot["recent_runs"]]
    seen: set[str] = set()
    choices: list[dict[str, Any]] = []
    lowered = query.strip().lower()
    for item in candidates:
        run_id = str(item.get("id") or "")
        if not run_id or run_id in seen:
            continue
        haystack = f"{run_id} {item.get('status', '')} {item.get('role', '')} {item.get('task', '')}".lower()
        if lowered and lowered not in haystack:
            continue
        seen.add(run_id)
        choices.append({
            "value": run_id,
            "label": f"{item.get('status', '')} {run_id}",
            "description": _trim(str(item.get("task") or item.get("role") or ""), 90),
            "append_space": False,
        })
        if len(choices) >= limit:
            break
    return choices


def _gateway_agent_choices(
    *,
    query: str,
    limit: int,
    gateway_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    runs = gateway_agent_snapshot(gateway_snapshot=gateway_snapshot, limit=limit)["running_agent_runs"]
    lowered = query.strip().lower()
    choices = []
    for item in runs:
        run_id = str(item.get("id") or "")
        haystack = f"{run_id} {item.get('platform', '')} {item.get('chat_id', '')} {item.get('user_id', '')}".lower()
        if lowered and lowered not in haystack:
            continue
        choices.append({
            "value": run_id,
            "label": f"{item.get('platform') or 'gateway'} {run_id}",
            "description": (
                f"{item.get('status', '')} "
                f"{float(item.get('duration_seconds') or 0.0):.1f}s"
            ).strip(),
            "append_space": False,
        })
        if len(choices) >= limit:
            break
    return choices


def _sub_agent_item(data: dict[str, Any], *, active: bool) -> dict[str, Any]:
    raw_status = str(data.get("status") or "")
    stop_requested = bool(data.get("stop_requested"))
    status = _normalize_sub_agent_status(raw_status, stop_requested=stop_requested)
    error = str(data.get("error_message") or data.get("error") or "")
    run_id = str(data.get("run_id") or data.get("id") or "")
    tool_counts = _tool_counts(data)
    return {
        "id": run_id,
        "run_id": run_id,
        "kind": "sub_agent",
        "status": status,
        "raw_status": raw_status,
        "role": str(data.get("role") or ""),
        "task": str(data.get("task") or ""),
        "task_preview": _trim(str(data.get("task") or ""), 160),
        "model": str(data.get("model") or ""),
        "tool_policy": data.get("tool_policy") or "",
        "active": bool(data.get("active", active)),
        "started_at": str(data.get("started_at") or ""),
        "finished_at": str(data.get("finished_at") or ""),
        "duration_seconds": round(float(data.get("duration_seconds") or data.get("duration") or 0.0), 3),
        "usage": dict(data.get("usage") or {}),
        "limits": dict(data.get("limits") or {}),
        "quota": dict(data.get("quota") or {}),
        "stop_requested": stop_requested,
        "error": error,
        "error_type": str(data.get("error_type") or ""),
        "attention_required": status == "failed",
        "tool_counts": tool_counts,
        "diagnostics": dict(data.get("diagnostics") or {}),
        "result_preview": _trim(str(data.get("result") or ""), 240),
    }


def _gateway_agent_item(data: dict[str, Any]) -> dict[str, Any]:
    stop_requested = bool(data.get("stop_requested"))
    raw_status = str(data.get("status") or "running")
    status = _normalize_gateway_status(raw_status, stop_requested=stop_requested)
    session_key = str(data.get("session_key") or data.get("id") or "")
    error = str(data.get("error") or "")
    return {
        "id": session_key,
        "session_key": session_key,
        "kind": "gateway_agent",
        "status": status,
        "raw_status": raw_status,
        "platform": str(data.get("platform") or ""),
        "chat_id": str(data.get("chat_id") or ""),
        "user_id": str(data.get("user_id") or ""),
        "started_at": str(data.get("started_at") or ""),
        "finished_at": str(data.get("finished_at") or ""),
        "duration_seconds": round(float(data.get("duration_seconds") or 0.0), 3),
        "stop_requested": stop_requested,
        "active_turn_id": str(data.get("active_turn_id") or ""),
        "pending_steers": int(data.get("pending_steers") or 0),
        "error": error,
        "attention_required": status == "failed" or bool(error),
    }


def _tool_counts(data: dict[str, Any]) -> dict[str, int]:
    return {
        "requested": _count_value(data.get("tool_calls")),
        "executed": _count_value(data.get("executed_tool_calls")),
        "denied": _count_value(data.get("denied_tool_calls")),
        "results": _count_value(data.get("tool_results")),
    }


def _count_value(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return len(value)
    return 0


def _normalize_sub_agent_status(status: str, *, stop_requested: bool) -> str:
    value = status.strip().lower()
    if stop_requested and value == "running":
        return "stopping"
    if value in {"running", "completed"}:
        return value
    if value in {"cancelled", "canceled", "stopped"}:
        return "stopped"
    if value in {"stopping"}:
        return "stopping"
    return "failed" if value else "failed"


def _normalize_gateway_status(status: str, *, stop_requested: bool) -> str:
    value = status.strip().lower()
    if stop_requested and value == "running":
        return "stopping"
    if value in PUBLIC_STATUSES:
        return value
    if value in {"cancelled", "canceled"}:
        return "stopped"
    return "failed" if value else "running"


def _normalize_kind(kind: str) -> str:
    value = kind.strip().lower()
    aliases = {
        "agents": "sub_agent",
        "sub_agents": "sub_agent",
        "process": "background_process",
        "processes": "background_process",
        "background_processes": "background_process",
        "gateway": "gateway_agent",
        "gateway_agents": "gateway_agent",
    }
    return aliases.get(value, value)


def _trim(text: str, limit: int) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
