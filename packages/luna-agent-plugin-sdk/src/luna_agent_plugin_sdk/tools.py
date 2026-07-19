"""Stable tool registration and artifact contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal


ResourceKind = Literal["filesystem", "network"]


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


@dataclass
class ToolArtifact:
    kind: str
    name: str = ""
    mime_type: str = ""
    data: str = ""
    uri: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def safe_summary(self) -> dict[str, Any]:
        uri_scheme = self.uri.partition(":")[0] if ":" in self.uri else ""
        return {
            "kind": self.kind,
            "name": self.name,
            "mime_type": self.mime_type,
            "encoded_size": len(self.data),
            "has_data": bool(self.data),
            "has_uri": bool(self.uri),
            "uri_scheme": uri_scheme,
        }


@dataclass
class ToolHandlerOutput:
    text: str = ""
    artifacts: list[ToolArtifact] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False


@dataclass
class ToolEntry:
    name: str
    description: str
    schema: dict
    handler: Callable[..., Awaitable[Any]]
    toolset: str = "general"
    permission_category: str = "default"
    tags: list[str] = field(default_factory=list)
    risk_level: str = "low"
    usage_hint: str = ""
    check_fn: Callable[[], bool] | None = None
    availability_reason_fn: Callable[[], str] | None = None
    precheck: Callable[[dict[str, Any]], str | None] | None = None
    approval_mode: str = "inherit"
    approval_mode_resolver: Callable[[dict[str, Any]], str] | None = None
    resource_resolver: Callable[[dict[str, Any]], list[Any]] | None = None
    idempotent: bool | None = None
    is_parallel_safe: bool = True
    is_destructive: bool = False
    report_as_tool: bool = True
    timeout_seconds: float | None = None
    timeout_resolver: Callable[[dict[str, Any]], float | None] | None = None
