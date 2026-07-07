"""Execution mode policy: sandbox hard boundaries plus permission gates."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

ExecutionMode = Literal["guarded", "standard", "trusted", "sovereign"]
PermissionDecision = Literal["allow", "ask", "deny"]
NetworkPolicy = Literal["deny", "ask", "allow"]

VALID_EXECUTION_MODES: set[str] = {"guarded", "standard", "trusted", "sovereign"}
VALID_PERMISSION_DECISIONS: set[str] = {"allow", "ask", "deny"}
PERMISSION_CATEGORIES: tuple[str, ...] = (
    "default",
    "read",
    "search",
    "write",
    "bash",
    "background",
    "network",
    "destructive",
)
VALID_PERMISSION_CATEGORIES: set[str] = set(PERMISSION_CATEGORIES)

MODE_DESCRIPTIONS: dict[str, str] = {
    "guarded": "conservative analysis mode",
    "standard": "balanced daily-use mode",
    "trusted": "trusted local project mode",
    "sovereign": "high-permission mode",
}


@dataclass(frozen=True)
class SandboxProfile:
    kind: str = "tool-enforced"
    path_roots_enforced: bool = True
    blocked_patterns_enforced: bool = True
    bash_path_restrict: bool = True
    file_write_limit_enforced: bool = True
    hard_prechecks_enforced: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class NetworkProfile:
    tool_permission: PermissionDecision = "deny"
    bash_network: PermissionDecision = "deny"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class GrantProfile:
    categories: tuple[str, ...] = ("write", "bash", "background", "network", "destructive", "all")
    scope: str = "turn"

    def as_dict(self) -> dict:
        return {
            "categories": list(self.categories),
            "scope": self.scope,
        }


@dataclass(frozen=True)
class AuditProfile:
    enabled: bool = True
    decisions: bool = True
    results: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionModeProfile:
    name: ExecutionMode
    label: str
    description: str
    tool_permissions: dict[str, PermissionDecision]
    sandbox: SandboxProfile = field(default_factory=SandboxProfile)
    network: NetworkProfile = field(default_factory=NetworkProfile)
    grants: GrantProfile = field(default_factory=GrantProfile)
    audit: AuditProfile = field(default_factory=AuditProfile)
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "tool_permissions": dict(self.tool_permissions),
            "sandbox": self.sandbox.as_dict(),
            "network": self.network.as_dict(),
            "grants": self.grants.as_dict(),
            "audit": self.audit.as_dict(),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ExecutionPolicy:
    mode: ExecutionMode = "standard"
    permissions: dict[str, PermissionDecision] = field(default_factory=dict)
    network: NetworkPolicy = "deny"
    isolation: str = "tool-enforced"
    warnings: tuple[str, ...] = ()
    profile: ExecutionModeProfile | None = None
    overrides: dict[str, dict[str, PermissionDecision]] = field(default_factory=dict)

    def permission_for(self, category: str) -> PermissionDecision:
        return self.permissions.get(category, self.permissions.get("default", "ask"))

    def explain_permission(self, category: str) -> dict:
        decision = self.permission_for(category)
        required_allow = category if decision == "ask" else ""
        if decision == "allow":
            message = f"{self.mode} mode allows {category} tools."
        elif decision == "deny":
            message = (
                f"{category} tools are denied by execution mode '{self.mode}'."
            )
        else:
            message = (
                f"{category} tools require authorization in execution mode '{self.mode}'. "
                f"Send /allow {category} or /allow all to enable for this turn."
            )
        return {
            "category": category,
            "decision": decision,
            "required_allow": required_allow,
            "message": message,
        }

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "description": self.profile.description if self.profile else MODE_DESCRIPTIONS[self.mode],
            "permissions": dict(self.permissions),
            "network": self.network,
            "isolation": self.isolation,
            "warnings": list(self.warnings),
            "profile": self.profile.as_dict() if self.profile else None,
            "overrides": {
                "tool_permissions": dict(self.overrides.get("tool_permissions", {})),
            },
        }


MODE_PROFILES: dict[str, ExecutionModeProfile] = {
    "guarded": ExecutionModeProfile(
        name="guarded",
        label="Guarded",
        description="conservative analysis mode",
        tool_permissions={
            "default": "deny",
            "read": "allow",
            "search": "allow",
            "write": "deny",
            "bash": "deny",
            "background": "deny",
            "network": "deny",
            "destructive": "deny",
        },
        network=NetworkProfile(tool_permission="deny", bash_network="deny"),
    ),
    "standard": ExecutionModeProfile(
        name="standard",
        label="Standard",
        description="balanced daily-use mode",
        tool_permissions={
            "default": "ask",
            "read": "allow",
            "search": "allow",
            "write": "ask",
            "bash": "ask",
            "background": "ask",
            "network": "ask",
            "destructive": "ask",
        },
        network=NetworkProfile(tool_permission="ask", bash_network="deny"),
    ),
    "trusted": ExecutionModeProfile(
        name="trusted",
        label="Trusted",
        description="trusted local project mode",
        tool_permissions={
            "default": "ask",
            "read": "allow",
            "search": "allow",
            "write": "allow",
            "bash": "allow",
            "background": "ask",
            "network": "ask",
            "destructive": "ask",
        },
        network=NetworkProfile(tool_permission="ask", bash_network="ask"),
    ),
    "sovereign": ExecutionModeProfile(
        name="sovereign",
        label="Sovereign",
        description="high-permission mode",
        tool_permissions={
            "default": "allow",
            "read": "allow",
            "search": "allow",
            "write": "allow",
            "bash": "allow",
            "background": "allow",
            "network": "allow",
            "destructive": "ask",
        },
        network=NetworkProfile(tool_permission="allow", bash_network="ask"),
        warnings=("sovereign mode broadly allows tool actions inside configured sandbox roots",),
    ),
}


def resolve_execution_policy(settings) -> ExecutionPolicy:
    mode = _normalize_mode(getattr(settings, "execution_mode", "standard"))
    profile = _profile_for_mode(mode)
    overrides = _normalize_policy_overrides(
        getattr(settings, "execution_policy_overrides", {})
    )
    permissions = dict(profile.tool_permissions)
    permissions.update(overrides.get("tool_permissions", {}))
    network = _effective_network_policy(profile, settings)

    # Hard-deny secret and protected path rules live in sandbox/precheck layers and
    # cannot be relaxed by this policy.
    return ExecutionPolicy(
        mode=mode,  # type: ignore[arg-type]
        permissions=permissions,
        network=network,
        isolation=profile.sandbox.kind,
        warnings=profile.warnings,
        profile=profile,
        overrides=overrides,
    )


def resolve_execution_policy_for_mode(settings, mode: object) -> ExecutionPolicy:
    """Resolve a policy for a runtime-selected mode without mutating settings."""

    class _ModeSettings:
        def __init__(self, base, execution_mode: str) -> None:
            self._base = base
            self.execution_mode = execution_mode

        def __getattr__(self, name: str):
            return getattr(self._base, name)

    return resolve_execution_policy(_ModeSettings(settings, _normalize_mode(mode)))


def _profile_for_mode(mode: str) -> ExecutionModeProfile:
    return MODE_PROFILES.get(mode, MODE_PROFILES["standard"])


def _effective_network_policy(profile: ExecutionModeProfile, settings) -> NetworkPolicy:
    if profile.name == "guarded":
        return "deny"
    if bool(getattr(settings, "bash_allow_network", False)):
        return "allow"
    return profile.network.tool_permission  # type: ignore[return-value]


def _normalize_mode(value: object) -> str:
    mode = str(value or "standard").strip().lower()
    return mode if mode in VALID_EXECUTION_MODES else "standard"


def _normalize_policy_overrides(value: object) -> dict[str, dict[str, PermissionDecision]]:
    tool_permissions: dict[str, PermissionDecision] = {}
    if not isinstance(value, dict):
        return {"tool_permissions": tool_permissions}

    _merge_permission_overrides(value, tool_permissions)
    nested = value.get("tool_permissions")
    if isinstance(nested, dict):
        _merge_permission_overrides(nested, tool_permissions)
    return {"tool_permissions": tool_permissions}


def _merge_permission_overrides(
    source: dict,
    target: dict[str, PermissionDecision],
) -> None:
    for key, value in source.items():
        category = str(key).strip().lower()
        decision = str(value).strip().lower()
        if category not in VALID_PERMISSION_CATEGORIES:
            continue
        if decision not in VALID_PERMISSION_DECISIONS:
            continue
        target[category] = decision  # type: ignore[assignment]
