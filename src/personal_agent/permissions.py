"""Runtime permission grants with per-turn and temporary scopes."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

DEFAULT_TEMPORARY_GRANT_TTL_SECONDS = 24 * 60 * 60


def temporary_grant_ttl_seconds(settings_or_agent: Any = None) -> int:
    value = getattr(settings_or_agent, "permission_temporary_grant_ttl_seconds", None)
    if value is None:
        minutes = getattr(settings_or_agent, "permission_grant_ttl_minutes", None)
        if minutes is not None:
            try:
                value = float(minutes) * 60
            except (TypeError, ValueError):
                return DEFAULT_TEMPORARY_GRANT_TTL_SECONDS
        else:
            hours = getattr(settings_or_agent, "permission_temporary_grant_ttl_hours", 24)
            try:
                value = float(hours) * 60 * 60
            except (TypeError, ValueError):
                return DEFAULT_TEMPORARY_GRANT_TTL_SECONDS
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return DEFAULT_TEMPORARY_GRANT_TTL_SECONDS
    return max(1, seconds)


def confirm_timeout_seconds(settings_or_agent: Any = None) -> int:
    value = getattr(settings_or_agent, "permission_confirm_timeout_seconds", 120)
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return 120
    return max(1, seconds)


def set_agent_permission_defaults(agent: Any, settings: Any) -> None:
    try:
        agent._permission_temporary_grant_ttl_seconds = temporary_grant_ttl_seconds(settings)
        agent._permission_confirm_timeout_seconds = confirm_timeout_seconds(settings)
    except Exception:
        return


def prepare_turn_grants(agent: Any) -> None:
    """Reset only per-turn grants and prune expired temporary grants."""
    if agent is None:
        return
    _ensure_set(agent, "_turn_grants").clear()
    # Compatibility: old code/tests still inspect this as the per-turn grant set.
    _ensure_set(agent, "_destructive_allowed").clear()
    prune_expired_temporary_grants(agent)


def add_turn_grants(agent: Any, *categories: str) -> set[str]:
    tokens = _clean_categories(categories)
    if agent is None or not tokens:
        return set()
    grants = _ensure_set(agent, "_turn_grants")
    legacy = _ensure_set(agent, "_destructive_allowed")
    added: set[str] = set()
    for token in tokens:
        if token not in grants:
            added.add(token)
        grants.add(token)
        legacy.add(token)
    return added


def remove_turn_grants(agent: Any, categories: set[str]) -> None:
    if agent is None or not categories:
        return
    for attr in ("_turn_grants", "_destructive_allowed"):
        grants = getattr(agent, attr, None)
        if grants is None:
            continue
        for token in categories:
            try:
                grants.discard(token)
            except AttributeError:
                break


def add_temporary_grant(
    agent: Any,
    category: str,
    *,
    ttl_seconds: int | None = None,
    now: float | None = None,
) -> float:
    token = _clean_category(category)
    if not token:
        return 0.0
    grants = _ensure_dict(agent, "_temporary_grants")
    ttl = int(ttl_seconds or temporary_grant_ttl_seconds(agent))
    expires_at = float(now if now is not None else time.time()) + max(1, ttl)
    grants[token] = expires_at
    return expires_at


def remove_temporary_grant(agent: Any, category: str) -> bool:
    token = _clean_category(category)
    grants = getattr(agent, "_temporary_grants", None)
    if not token or not isinstance(grants, dict):
        return False
    if token == "all":
        changed = bool(grants)
        grants.clear()
        return changed
    return grants.pop(token, None) is not None


def prune_expired_temporary_grants(agent: Any, *, now: float | None = None) -> None:
    grants = getattr(agent, "_temporary_grants", None)
    if not isinstance(grants, dict):
        return
    current = float(now if now is not None else time.time())
    for key, expires_at in list(grants.items()):
        try:
            expired = float(expires_at) <= current
        except (TypeError, ValueError):
            expired = True
        if expired:
            grants.pop(key, None)


def matching_permission_grant(agent: Any, category: str, *, now: float | None = None) -> tuple[str, str, float]:
    """Return (grant_token, scope, expires_at). Empty token means no match."""
    if agent is None:
        return "", "", 0.0
    current = float(now if now is not None else time.time())
    temporary = getattr(agent, "_temporary_grants", None)
    if isinstance(temporary, dict):
        for token in ("all", category):
            expires_at = temporary.get(token)
            if expires_at is None:
                continue
            try:
                expires = float(expires_at)
            except (TypeError, ValueError):
                temporary.pop(token, None)
                continue
            if expires > current:
                return token, "temporary", expires
            temporary.pop(token, None)
    for attr in ("_turn_grants", "_destructive_allowed"):
        grants = getattr(agent, attr, None)
        if grants is None:
            continue
        if "all" in grants:
            return "all", "turn", 0.0
        if category in grants:
            return category, "turn", 0.0
    return "", "", 0.0


def temporary_grants_snapshot(agent: Any, *, now: float | None = None) -> list[dict[str, Any]]:
    prune_expired_temporary_grants(agent, now=now)
    grants = getattr(agent, "_temporary_grants", None)
    if not isinstance(grants, dict):
        return []
    return [
        {
            "category": str(category),
            "expires_at": float(expires_at),
            "expires_at_iso": format_expiry(float(expires_at)),
        }
        for category, expires_at in sorted(grants.items())
    ]


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


def _ensure_set(agent: Any, attr: str) -> set[str]:
    value = getattr(agent, attr, None)
    if not isinstance(value, set):
        value = set()
        setattr(agent, attr, value)
    return value


def _ensure_dict(agent: Any, attr: str) -> dict[str, float]:
    value = getattr(agent, attr, None)
    if not isinstance(value, dict):
        value = {}
        setattr(agent, attr, value)
    return value


def _clean_categories(categories: tuple[str, ...] | list[str] | set[str]) -> set[str]:
    return {item for item in (_clean_category(category) for category in categories) if item}


def _clean_category(category: str) -> str:
    return str(category or "").strip().lower()
