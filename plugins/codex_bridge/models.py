"""Persistent models for Codex-driven plugin development sessions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class DevelopmentStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_CODEX = "waiting_codex"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"


class DevelopmentEventType(StrEnum):
    TURN_STARTED = "turn_started"
    ASSISTANT_MESSAGE = "assistant_message"
    PROGRESS = "progress"
    REQUEST_USER_INPUT = "request_user_input"
    APPROVAL_REQUESTED = "approval_requested"
    TURN_COMPLETED = "turn_completed"
    ERROR = "error"
    PROCESS_RESTARTED = "process_restarted"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class DevelopmentEvent:
    event_id: str
    plugin_id: str
    event_type: str
    text: str
    thread_id: str = ""
    turn_id: str = ""
    created_at: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DevelopmentSession:
    plugin_id: str
    thread_id: str = ""
    model: str = ""
    model_provider: str = ""
    workspace_path: str = ""
    brief_path: str = ""
    spec_revision: str = ""
    status: str = DevelopmentStatus.CREATED.value
    current_turn_id: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_result: str = ""
    last_error: str = ""
    generation: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DevelopmentSession":
        fields = {key: value[key] for key in cls.__dataclass_fields__ if key in value}
        fields["events"] = list(fields.get("events") or [])
        return cls(**fields)
