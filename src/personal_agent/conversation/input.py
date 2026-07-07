"""Structured conversation input shared by CLI, gateway, and future desktop clients."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from personal_agent.models.messages import AttachmentRef, MessageEnvelope, MessagePart, SessionSource


@dataclass
class ConversationInput:
    """User input before multimodal resolution."""

    text: str = ""
    source: SessionSource | None = None
    parts: list[MessagePart] = field(default_factory=list)
    attachments: list[AttachmentRef] = field(default_factory=list)
    envelope: MessageEnvelope | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def text_only(cls, text: str, *, source: SessionSource | None = None) -> "ConversationInput":
        return cls(text=str(text or ""), source=source)

    @classmethod
    def from_envelope(cls, envelope: MessageEnvelope) -> "ConversationInput":
        return cls(
            text=envelope.render_text(),
            source=envelope.source,
            parts=list(envelope.parts or []),
            attachments=list(envelope.attachments or []),
            envelope=envelope,
            metadata=dict(envelope.metadata or {}),
        )

    def attachment_kinds(self) -> list[str]:
        return sorted({str(item.kind or "unknown") for item in self.attachments})


def ensure_conversation_input(
    value: str | ConversationInput,
    *,
    source: SessionSource | None = None,
) -> ConversationInput:
    if isinstance(value, ConversationInput):
        return value
    return ConversationInput.text_only(str(value or ""), source=source)
