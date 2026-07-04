"""Execution mode policy: sandbox hard boundaries plus permission gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ExecutionMode = Literal["guarded", "standard", "trusted", "sovereign"]
PermissionDecision = Literal["allow", "ask", "deny"]
NetworkPolicy = Literal["deny", "ask", "allow"]

VALID_EXECUTION_MODES: set[str] = {"guarded", "standard", "trusted", "sovereign"}

MODE_DESCRIPTIONS: dict[str, str] = {
    "guarded": "conservative analysis mode",
    "standard": "balanced daily-use mode",
    "trusted": "trusted local project mode",
    "sovereign": "high-permission mode",
}


@dataclass(frozen=True)
class ExecutionPolicy:
    mode: ExecutionMode = "standard"
    permissions: dict[str, PermissionDecision] = field(default_factory=dict)
    network: NetworkPolicy = "deny"
    isolation: str = "policy-only"
    warnings: tuple[str, ...] = ()

    def permission_for(self, category: str) -> PermissionDecision:
        return self.permissions.get(category, self.permissions.get("default", "ask"))

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "description": MODE_DESCRIPTIONS[self.mode],
            "permissions": dict(self.permissions),
            "network": self.network,
            "isolation": self.isolation,
            "warnings": list(self.warnings),
        }


def resolve_execution_policy(settings) -> ExecutionPolicy:
    mode = _normalize_mode(getattr(settings, "execution_mode", "standard"))
    permissions: dict[str, PermissionDecision]
    network: NetworkPolicy
    warnings: list[str] = []

    if mode == "guarded":
        permissions = {
            "default": "deny",
            "read": "allow",
            "search": "allow",
            "write": "deny",
            "bash": "deny",
            "background": "deny",
            "network": "deny",
            "destructive": "deny",
        }
        network = "deny"
    elif mode == "standard":
        permissions = {
            "default": "ask",
            "read": "allow",
            "search": "allow",
            "write": "ask",
            "bash": "ask",
            "background": "ask",
            "network": "deny",
            "destructive": "ask",
        }
        network = "allow" if bool(getattr(settings, "bash_allow_network", False)) else "deny"
    elif mode == "trusted":
        permissions = {
            "default": "ask",
            "read": "allow",
            "search": "allow",
            "write": "allow",
            "bash": "allow",
            "background": "ask",
            "network": "ask",
            "destructive": "ask",
        }
        network = "allow" if bool(getattr(settings, "bash_allow_network", False)) else "ask"
    else:
        permissions = {
            "default": "allow",
            "read": "allow",
            "search": "allow",
            "write": "allow",
            "bash": "allow",
            "background": "allow",
            "network": "allow",
            "destructive": "ask",
        }
        network = "allow" if bool(getattr(settings, "bash_allow_network", False)) else "ask"
        warnings.append("sovereign mode broadly allows tool actions inside configured sandbox roots")

    # Hard-deny secret and protected path rules live in sandbox/precheck layers and
    # cannot be relaxed by this policy.
    return ExecutionPolicy(
        mode=mode,  # type: ignore[arg-type]
        permissions=permissions,
        network=network,
        warnings=tuple(warnings),
    )


def _normalize_mode(value: object) -> str:
    mode = str(value or "standard").strip().lower()
    return mode if mode in VALID_EXECUTION_MODES else "standard"
