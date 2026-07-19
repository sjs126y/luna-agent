"""Public contracts for generation-owned active plugin runners."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

ActiveRun = Callable[[Any], Awaitable[None]]
ActiveLifecycleCallback = Callable[[Any], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class ActiveConversationIntent:
    """Internal initiative handed to the host conversation runtime."""

    intent_id: str
    session_key: str
    kind: str
    instruction: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    request_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("intent_id", "session_key", "kind", "instruction"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"active conversation intent {name} is required")
        object.__setattr__(self, "intent_id", str(self.intent_id).strip())
        object.__setattr__(self, "session_key", str(self.session_key).strip())
        object.__setattr__(self, "kind", str(self.kind).strip())
        object.__setattr__(self, "instruction", str(self.instruction).strip())
        object.__setattr__(self, "request_id", str(self.request_id or "").strip())
        object.__setattr__(self, "evidence", dict(self.evidence or {}))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))


@dataclass(frozen=True, slots=True)
class ConversationStatus:
    """Bounded read-only session activity exposed to active plugins."""

    session_key: str
    busy: bool = False
    queued_count: int = 0
    last_user_at: str = ""
    last_assistant_at: str = ""
    recent_user_messages: tuple[str, ...] = ()


class ActiveRestartPolicy(StrEnum):
    NEVER = "never"
    ON_FAILURE = "on_failure"
    ALWAYS = "always"


class ActiveRunnerState(StrEnum):
    DISABLED = "disabled"
    STARTING = "starting"
    READY = "ready"
    ACTIVE = "active"
    QUIESCING = "quiescing"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ActiveResourceRequest:
    tools: tuple[str, ...] = ()
    mcp: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    required_mcp_servers: tuple[str, ...] = ()
    optional_mcp_servers: tuple[str, ...] = ()
    llm: bool = False
    conversation: bool = False
    delivery: bool = False
    events: bool = False
    artifacts: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", _names(self.tools, "tools"))
        object.__setattr__(self, "required_mcp_servers", _names(
            self.required_mcp_servers, "required_mcp_servers"
        ))
        object.__setattr__(self, "optional_mcp_servers", _names(
            self.optional_mcp_servers, "optional_mcp_servers"
        ))
        normalized = {
            str(server or "").strip(): _names(tools, f"mcp.{server}")
            for server, tools in dict(self.mcp or {}).items()
        }
        if any(not name for name in normalized):
            raise ValueError("Active MCP server names must not be empty")
        object.__setattr__(self, "mcp", MappingProxyType(normalized))
        declared = set(normalized)
        missing = (set(self.required_mcp_servers) | set(self.optional_mcp_servers)) - declared
        if missing:
            raise ValueError(
                "Active MCP readiness servers must also declare tools: "
                + ", ".join(sorted(missing))
            )
        overlap = set(self.required_mcp_servers) & set(self.optional_mcp_servers)
        if overlap:
            raise ValueError(
                "Active MCP servers cannot be both required and optional: "
                + ", ".join(sorted(overlap))
            )

    def safe_summary(self) -> dict[str, Any]:
        return {
            "tools": list(self.tools),
            "mcp": {server: list(tools) for server, tools in self.mcp.items()},
            "required_mcp_servers": list(self.required_mcp_servers),
            "optional_mcp_servers": list(self.optional_mcp_servers),
            "llm": self.llm,
            "conversation": self.conversation,
            "delivery": self.delivery,
            "events": self.events,
            "artifacts": self.artifacts,
        }


@dataclass(frozen=True, slots=True)
class ActiveRegistration:
    run: ActiveRun
    resources: ActiveResourceRequest = field(default_factory=ActiveResourceRequest)
    restart_policy: ActiveRestartPolicy = ActiveRestartPolicy.ON_FAILURE
    startup_timeout: float = 20.0
    shutdown_timeout: float = 20.0
    on_quiesce: ActiveLifecycleCallback | None = None
    on_resume: ActiveLifecycleCallback | None = None
    on_stop: ActiveLifecycleCallback | None = None

    def __post_init__(self) -> None:
        if not callable(self.run):
            raise TypeError("Active plugin run must be callable")
        if self.startup_timeout <= 0 or self.shutdown_timeout <= 0:
            raise ValueError("Active plugin timeouts must be positive")
        for name in ("on_quiesce", "on_resume", "on_stop"):
            callback = getattr(self, name)
            if callback is not None and not callable(callback):
                raise TypeError(f"Active plugin {name} must be callable")


def _names(values, label: str) -> tuple[str, ...]:
    result = tuple(dict.fromkeys(str(value or "").strip() for value in values))
    if any(not value for value in result):
        raise ValueError(f"Active resource {label} must contain non-empty names")
    return result
