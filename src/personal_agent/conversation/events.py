"""Conversation event model for terminal and future desktop renderers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ConversationEventType = Literal[
    "turn_start",
    "llm_start",
    "assistant_delta",
    "llm_end",
    "assistant_message",
    "tool_start",
    "tool_end",
    "retry",
    "compression",
    "stop",
    "error",
    "turn_end",
]


@dataclass(slots=True)
class ConversationEvent:
    type: ConversationEventType
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class ConversationEventSink:
    """Async event sink protocol implemented as a base class for convenience."""

    async def emit(self, event: ConversationEvent) -> None:
        raise NotImplementedError


class EventRecorder(ConversationEventSink):
    """Collect events while optionally forwarding them to another sink."""

    def __init__(self, forward: ConversationEventSink | None = None) -> None:
        self.events: list[ConversationEvent] = []
        self.forward = forward

    async def emit(self, event: ConversationEvent) -> None:
        self.events.append(event)
        if self.forward is not None:
            await self.forward.emit(event)


async def emit_event(
    sink: ConversationEventSink | None,
    event_type: ConversationEventType,
    message: str = "",
    **data: Any,
) -> None:
    if sink is None:
        return
    await sink.emit(ConversationEvent(type=event_type, message=message, data=data))

