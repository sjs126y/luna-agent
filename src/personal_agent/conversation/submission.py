"""Application-level contracts for submitting work to the conversation runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
import uuid

from personal_agent.conversation.input import ConversationInput


class SubmissionOrigin(StrEnum):
    """Trusted runtime entrypoint that created a submission."""

    GATEWAY = "gateway"
    TUI = "tui"
    CLI = "cli"
    CRON = "cron"
    PLUGIN = "plugin"
    SYSTEM = "system"


class ResponseMode(StrEnum):
    """How the final logical response leaves the conversation runtime."""

    RETURN_ONLY = "return_only"
    DELIVER = "deliver"
    SILENT = "silent"


class SubmissionKind(StrEnum):
    CONVERSATION = "conversation"
    COMMAND = "command"
    CONTROL = "control"


class SubmissionStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class SubmissionRequest:
    """A normalized request accepted by ``ConversationCoordinator``."""

    session_key: str
    input: ConversationInput
    origin: SubmissionOrigin
    response_mode: ResponseMode
    request_id: str = field(default_factory=lambda: f"sub_{uuid.uuid4().hex}")
    owner_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    event_sink: Any = field(default=None, repr=False, compare=False)
    confirm: Any = field(default=None, repr=False, compare=False)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        session_key = str(self.session_key or "").strip()
        request_id = str(self.request_id or "").strip()
        if not session_key:
            raise ValueError("session_key is required")
        if not request_id:
            raise ValueError("request_id is required")
        object.__setattr__(self, "session_key", session_key)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "owner_id", str(self.owner_id or "").strip())
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @classmethod
    def text(
        cls,
        *,
        session_key: str,
        text: str,
        origin: SubmissionOrigin,
        response_mode: ResponseMode,
        source=None,
        owner_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> "SubmissionRequest":
        return cls(
            session_key=session_key,
            input=ConversationInput.text_only(text, source=source),
            origin=origin,
            response_mode=response_mode,
            owner_id=owner_id,
            metadata=dict(metadata or {}),
        )


@dataclass(frozen=True, slots=True)
class SubmissionReceipt:
    request_id: str
    session_key: str
    accepted: bool
    status: SubmissionStatus
    queue_position: int = 0
    reason: str = ""
    accepted_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class SubmissionOutcome:
    request_id: str
    session_key: str
    status: SubmissionStatus
    kind: SubmissionKind = SubmissionKind.CONVERSATION
    response: str = ""
    error: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def succeeded(self) -> bool:
        return self.status == SubmissionStatus.COMPLETED


class SubmissionHandle:
    """Immediate receipt plus an awaitable final outcome."""

    __slots__ = ("receipt", "_future")

    def __init__(
        self,
        receipt: SubmissionReceipt,
        future: asyncio.Future[SubmissionOutcome],
    ) -> None:
        self.receipt = receipt
        self._future = future

    @property
    def request_id(self) -> str:
        return self.receipt.request_id

    @property
    def done(self) -> bool:
        return self._future.done()

    async def outcome(self) -> SubmissionOutcome:
        return await asyncio.shield(self._future)

    def cancel(self) -> bool:
        return self._future.cancel()
