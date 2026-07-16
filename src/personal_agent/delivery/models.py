from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
import uuid

from personal_agent.models.messages import OutboundMessage


class DeliveryKind(StrEnum):
    CONVERSATION = "conversation"
    COMMAND = "command"
    NOTIFICATION = "notification"
    APPROVAL = "approval"
    AUTH = "auth"
    SYSTEM = "system"

    @property
    def protected(self) -> bool:
        return self in {self.APPROVAL, self.AUTH, self.SYSTEM}


class DeliveryStatus(StrEnum):
    DELIVERED = "delivered"
    SUPPRESSED = "suppressed"
    FAILED = "failed"
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class DeliveryRequest:
    session_key: str
    message: OutboundMessage
    kind: DeliveryKind = DeliveryKind.CONVERSATION
    delivery_id: str = field(default_factory=lambda: f"del_{uuid.uuid4().hex}")
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlatformSendResult:
    success: bool
    message_id: str = ""
    error: str = ""
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    delivery_id: str
    session_key: str
    status: DeliveryStatus
    platform: str = ""
    chat_id: str = ""
    message_id: str = ""
    error: str = ""
    attempts: int = 0
    completed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def delivered(self) -> bool:
        return self.status == DeliveryStatus.DELIVERED
