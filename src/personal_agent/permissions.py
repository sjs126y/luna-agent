"""Shared duration helpers for session-scoped security grants."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def temporary_grant_ttl_seconds(settings_or_agent: Any = None) -> int:
    minutes = getattr(settings_or_agent, "permission_grant_ttl_minutes", 60)
    try:
        seconds = int(float(minutes) * 60)
    except (TypeError, ValueError):
        return 60 * 60
    return max(1, seconds)


def confirm_timeout_seconds(settings_or_agent: Any = None) -> int:
    value = getattr(settings_or_agent, "permission_confirm_timeout_seconds", 120)
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return 120
    return max(1, seconds)


def format_expiry(ts: float) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def format_grant_duration(seconds: int) -> str:
    value = max(1, int(seconds))
    if value % 3600 == 0:
        return f"{value // 3600}小时"
    if value % 60 == 0:
        return f"{value // 60}分钟"
    return f"{value}秒"
