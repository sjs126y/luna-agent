"""In-memory, per-session security state."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any

from personal_agent.security.models import (
    FileSystemRule,
    PermissionProfile,
    ResourceGrant,
    ResourceRequirement,
    SecurityContext,
    ToolGrant,
)
from personal_agent.security.modes import mode_preset, normalize_mode_id


@dataclass
class SecuritySessionState:
    mode_id: str
    tool_grants: dict[str, ToolGrant] = field(default_factory=dict)
    resource_grants: dict[str, ResourceGrant] = field(default_factory=dict)

    def clear_grants(self) -> None:
        self.tool_grants.clear()
        self.resource_grants.clear()

    def prune_expired(self, *, now: float | None = None) -> None:
        current = float(time.time() if now is None else now)
        self.tool_grants = {
            key: grant for key, grant in self.tool_grants.items() if grant.expires_at > current
        }
        self.resource_grants = {
            key: grant for key, grant in self.resource_grants.items() if grant.expires_at > current
        }

    def has_tool_grant(self, tool_key: str, *, now: float | None = None) -> bool:
        self.prune_expired(now=now)
        return tool_key in self.tool_grants

    def has_resource_grant(self, requirement: ResourceRequirement, *, now: float | None = None) -> bool:
        self.prune_expired(now=now)
        return requirement.key in self.resource_grants

    def grant_tool(self, tool_key: str, *, ttl_seconds: int, now: float | None = None) -> float:
        expires_at = float(time.time() if now is None else now) + max(1, int(ttl_seconds))
        self.tool_grants[tool_key] = ToolGrant(tool_key=tool_key, expires_at=expires_at)
        return expires_at

    def grant_resource(
        self,
        requirement: ResourceRequirement,
        *,
        ttl_seconds: int,
        now: float | None = None,
    ) -> float:
        expires_at = float(time.time() if now is None else now) + max(1, int(ttl_seconds))
        self.resource_grants[requirement.key] = ResourceGrant(requirement=requirement, expires_at=expires_at)
        return expires_at


class SecurityStateStore:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._states: dict[str, SecuritySessionState] = {}

    @property
    def grant_ttl_seconds(self) -> int:
        minutes = getattr(self.settings, "permission_grant_ttl_minutes", None)
        if minutes is not None:
            try:
                return max(60, int(minutes) * 60)
            except (TypeError, ValueError):
                pass
        hours = getattr(self.settings, "permission_temporary_grant_ttl_hours", 24)
        try:
            return max(60, int(float(hours) * 60 * 60))
        except (TypeError, ValueError):
            return 60 * 60

    def get(self, session_key: str) -> SecuritySessionState:
        if session_key not in self._states:
            self._states[session_key] = SecuritySessionState(
                mode_id=normalize_mode_id(getattr(self.settings, "execution_mode", "ask-first"))
            )
        return self._states[session_key]

    def set_mode(self, session_key: str, mode: object) -> SecuritySessionState:
        state = self.get(session_key)
        state.mode_id = normalize_mode_id(mode)
        state.clear_grants()
        return state

    def clear(self, session_key: str) -> None:
        self._states.pop(session_key, None)

    def move(self, old_key: str, new_key: str) -> None:
        state = self._states.pop(old_key, None)
        if state is not None:
            self._states[new_key] = state

    def context(self, session_key: str) -> SecurityContext:
        state = self.get(session_key)
        preset = mode_preset(state.mode_id)
        return SecurityContext(
            session_key=session_key,
            profile=_profile_for(self.settings, preset.profile),
            approval_policy=preset.approval_policy,
            state=state,
            mode_id=preset.id,
        )


def _profile_for(settings: Any, name: str) -> PermissionProfile:
    roots = tuple(Path(path).resolve() for path in (getattr(settings, "sandbox_roots", []) or []))
    access = "read" if name == "read-only" else "write"
    rules = tuple(FileSystemRule(path=root, access=access) for root in roots)
    return PermissionProfile(
        name=name,
        filesystem=rules,
        network_enabled=name == "trusted",
    )
