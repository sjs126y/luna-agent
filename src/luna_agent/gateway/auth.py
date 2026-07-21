"""Owner-only access policy for external gateway messages.

Platform login (QR code, bot token, or app credentials) authenticates the
bot account, not the person sending a message.  This module therefore keeps
the second boundary explicit: only configured owner IDs in direct messages
may enter the conversation runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


TRUSTED_INTERNAL_PLATFORMS = frozenset({"cli", "tui", "cron", "system", "plugin"})


@dataclass(frozen=True, slots=True)
class AccessDecision:
    allowed: bool
    reason_code: str


class OwnerAccessPolicy:
    """Small, stateless authorization boundary for gateway sources."""

    def __init__(self, config: Any, data_dir: Any | None = None) -> None:
        # ``data_dir`` remains accepted for source compatibility; this policy
        # is intentionally stateless.
        del data_dir
        self.enabled = bool(getattr(config, "auth_enabled", True))
        raw = getattr(config, "auth_owner_ids", {}) or {}
        self.owner_ids = _normalize_owner_ids(raw)

    def authorize(self, source: Any, *, internal: bool = False) -> AccessDecision:
        platform = str(getattr(source, "platform", "") or "").strip().lower()
        user_id = str(getattr(source, "user_id", "") or "").strip()
        chat_type = str(getattr(source, "chat_type", "dm") or "dm").strip().lower()

        if internal or platform in TRUSTED_INTERNAL_PLATFORMS:
            return AccessDecision(True, "trusted_internal")
        if not self.enabled:
            return AccessDecision(True, "authentication_disabled")
        if chat_type != "dm":
            return AccessDecision(False, "group_message_denied")
        if not platform or not user_id:
            return AccessDecision(False, "invalid_source")
        if user_id in self.owner_ids.get(platform, frozenset()):
            return AccessDecision(True, "owner_match")
        if platform not in self.owner_ids:
            return AccessDecision(False, "platform_owner_not_configured")
        return AccessDecision(False, "user_not_owner")

    def is_allowed(self, source: Any, *, internal: bool = False) -> bool:
        return self.authorize(source, internal=internal).allowed

    def check(self, user_id: str, text: str = "") -> tuple[bool, str | None]:
        """Legacy compatibility shim for integrations that only have a user id.

        Gateway authorization uses :meth:`authorize`, which includes platform
        and chat type.  This method deliberately cannot grant external access.
        """
        del text
        if not self.enabled:
            return True, None
        normalized = str(user_id or "").strip()
        allowed = any(normalized in values for values in self.owner_ids.values())
        return (True, None) if allowed else (False, "owner_auth_required")

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "configured_platforms": sorted(self.owner_ids),
            "configured_owner_count": sum(len(values) for values in self.owner_ids.values()),
        }


# Keep the old internal name importable for integrations. New code should use
# OwnerAccessPolicy explicitly.
AuthManager = OwnerAccessPolicy


def _normalize_owner_ids(raw: Any) -> dict[str, frozenset[str]]:
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, frozenset[str]] = {}
    for platform, values in raw.items():
        key = str(platform or "").strip().lower()
        if not key:
            continue
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple, set, frozenset)):
            continue
        normalized = frozenset(
            str(value or "").strip()
            for value in values
            if str(value or "").strip()
        )
        if normalized:
            result[key] = normalized
    return result
