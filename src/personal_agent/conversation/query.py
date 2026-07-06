"""Read-only conversation query facade."""

from __future__ import annotations

from typing import Any


class ConversationQueryService:
    """Stable read-only queries over conversation runtime state."""

    def __init__(self, service) -> None:
        self._service = service

    async def current_session(self, session_key: str, source) -> dict[str, Any]:
        session = self._service.session_store.get(session_key)
        if session is None:
            return {
                "session_key": session_key,
                "session_id": "",
                "session_short": "",
                "message_count": 0,
                "exists": False,
            }
        current_id = self._service.resolve_session_id(session.session_id)
        return {
            "session_key": session_key,
            "session_id": current_id,
            "session_short": current_id[:8],
            "message_count": len(await self._service.session_store.load_history(current_id)),
            "exists": True,
        }

    async def list_sessions(
        self,
        *,
        platform: str,
        user_id: str,
        current_key: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        sessions = await self._service.session_store.list_user_sessions(platform, user_id)
        return {
            "platform": platform,
            "user_id": user_id,
            "current_key": current_key,
            "limit": max(0, int(limit)),
            "items": sessions[: max(0, int(limit))],
            "total": len(sessions),
        }

    def recent_turn_reports(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._service.recent_turn_reports(limit)

    def turn_report_summary(self) -> dict[str, Any]:
        return self._service.turn_report_summary()

    def recent_tool_truth(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._service.recent_tool_truth(limit)

    def tool_truth_summary(self, limit: int = 10) -> dict[str, Any]:
        return self._service.tool_truth_summary(limit)

    async def recent_tool_runs(
        self,
        *,
        limit: int = 20,
        session_key: str | None = None,
    ) -> dict[str, Any]:
        normalized_limit = max(0, int(limit))
        items = await self._service.recent_tool_runs(
            limit=normalized_limit,
            session_key=session_key,
        )
        return {
            "scope": "session" if session_key else "all",
            "session_key": session_key or "",
            "limit": normalized_limit,
            "items": [_normalize_tool_run(item) for item in items],
        }

    async def tool_run_detail(self, run_id: int) -> dict[str, Any] | None:
        item = await self._service.get_tool_run(int(run_id))
        if item is None:
            return None
        return _normalize_tool_run(item)

    async def tool_run_summary(
        self,
        *,
        limit: int = 50,
        session_key: str | None = None,
    ) -> dict[str, Any]:
        normalized_limit = max(0, int(limit))
        if session_key:
            recent = await self._service.recent_tool_runs(
                limit=normalized_limit,
                session_key=session_key,
            )
            summary = _tool_run_summary(recent)
        else:
            summary = await self._service.tool_run_summary(limit=normalized_limit)
        return {
            "scope": "session" if session_key else "all",
            "session_key": session_key or "",
            "limit": normalized_limit,
            **summary,
        }


def _normalize_tool_run(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(item.get("id") or 0),
        "session_id": str(item.get("session_id") or ""),
        "session_key": str(item.get("session_key") or ""),
        "turn_id": str(item.get("turn_id") or ""),
        "tool_use_id": str(item.get("tool_use_id") or ""),
        "tool_name": str(item.get("tool_name") or ""),
        "status": str(item.get("status") or ""),
        "category": str(item.get("category") or ""),
        "duration": float(item.get("duration") or 0.0),
        "input_summary": str(item.get("input_summary") or ""),
        "output_summary": str(item.get("output_summary") or ""),
        "full_output": str(item.get("full_output") or ""),
        "output_truncated": bool(item.get("output_truncated", False)),
        "error": str(item.get("error") or ""),
        "guard_stage": str(item.get("guard_stage") or ""),
        "reason_code": str(item.get("reason_code") or ""),
        "permission_category": str(item.get("permission_category") or ""),
        "permission_decision": str(item.get("permission_decision") or ""),
        "required_allow": str(item.get("required_allow") or ""),
        "execution_mode": str(item.get("execution_mode") or ""),
        "grant_matched": str(item.get("grant_matched") or ""),
        "created_at": float(item.get("created_at") or 0.0),
    }


def _tool_run_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return _empty_tool_run_summary()
    tool_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    truncated = 0
    for item in items:
        tool_name = str(item.get("tool_name") or "")
        status = str(item.get("status") or "")
        category = str(item.get("category") or "")
        if tool_name:
            tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
        if status:
            status_counts[status] = status_counts.get(status, 0) + 1
        if category:
            category_counts[category] = category_counts.get(category, 0) + 1
        if item.get("output_truncated"):
            truncated += 1
    return {
        "inspected": len(items),
        "tool_counts": dict(sorted(tool_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "category_counts": dict(sorted(category_counts.items())),
        "denied": int(status_counts.get("denied", 0)),
        "failed": int(status_counts.get("error", 0)),
        "timeouts": int(status_counts.get("timeout", 0)),
        "truncated": truncated,
    }


def _empty_tool_run_summary() -> dict[str, Any]:
    return {
        "inspected": 0,
        "tool_counts": {},
        "status_counts": {},
        "category_counts": {},
        "denied": 0,
        "failed": 0,
        "timeouts": 0,
        "truncated": 0,
    }
