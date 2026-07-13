"""Core security values shared by commands, tools, and transports."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ApprovalPolicy = Literal["on-request", "never"]
ToolApprovalMode = Literal["auto", "cached", "prompt", "deny"]
ResourceKind = Literal["filesystem", "network"]
FileAccess = Literal["read", "write", "deny"]


@dataclass(frozen=True)
class FileSystemRule:
    path: Path
    access: FileAccess

    def matches(self, candidate: Path) -> bool:
        root = self.path.resolve()
        target = candidate.resolve()
        return target == root or root in target.parents


@dataclass(frozen=True)
class PermissionProfile:
    name: str
    filesystem: tuple[FileSystemRule, ...] = ()
    network_enabled: bool = False

    def filesystem_access(self, path: Path) -> FileAccess:
        matches = [rule for rule in self.filesystem if rule.matches(path)]
        if not matches:
            return "deny"
        matches.sort(key=lambda rule: len(rule.path.resolve().parts), reverse=True)
        return matches[0].access

    def allows(self, requirement: ResourceRequirement) -> bool:
        if requirement.kind == "network":
            return self.network_enabled
        access = self.filesystem_access(Path(requirement.resource))
        if access == "deny":
            return False
        return requirement.access == "read" or access == "write"


@dataclass(frozen=True)
class ResourceRequirement:
    kind: ResourceKind
    resource: str
    access: str = "read"
    reason: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.access}:{self.resource}"

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "resource": self.resource,
            "access": self.access,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ToolGrant:
    tool_key: str
    expires_at: float


@dataclass(frozen=True)
class ResourceGrant:
    requirement: ResourceRequirement
    expires_at: float


@dataclass
class SecurityContext:
    session_key: str
    profile: PermissionProfile
    approval_policy: ApprovalPolicy
    state: Any
    mode_id: str
    tool_approval_default_external: ToolApprovalMode = "cached"
    tool_approval_tools: dict[str, ToolApprovalMode] = field(default_factory=dict)
    tool_approval_mcp_servers: dict[str, ToolApprovalMode] = field(default_factory=dict)


@dataclass(frozen=True)
class SecurityDecision:
    decision: Literal["allow", "ask", "deny"]
    reason_code: str
    message: str
    tool_approval_mode: ToolApprovalMode = "auto"
    missing_resources: tuple[ResourceRequirement, ...] = ()
    tool_grant_matched: bool = False
    resource_grants_matched: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


@dataclass(frozen=True)
class PreparedToolCall:
    tool_use_id: str
    name: str
    input: dict[str, Any]
    tool_key: str
    source: str
    approval_mode: ToolApprovalMode
    approval_inherited: bool = False
    resources: tuple[ResourceRequirement, ...] = ()
    idempotent: bool = False
    parallel_safe: bool = False
