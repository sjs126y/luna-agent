"""ToolEntry — what gets registered."""

from dataclasses import dataclass, field
from collections.abc import Callable, Awaitable
from typing import Any


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
    schema: dict                # OpenAI/Anthropic function schema
    handler: Callable[..., Awaitable[Any]]
    toolset: str = "general"    # "web" | "terminal" | "memory" | ...
    permission_category: str = "default"
    tags: list[str] = field(default_factory=list)
    risk_level: str = "low"
    usage_hint: str = ""
    check_fn: Callable[[], bool] | None = None  # dependency check → True/False
    precheck: Callable[[dict[str, Any]], str | None] | None = None
    is_parallel_safe: bool = True
    is_destructive: bool = False
