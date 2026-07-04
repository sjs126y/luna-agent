"""Conversation event model for terminal and future desktop renderers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ConversationEventType = Literal[
    "turn_start",
    "llm_start",
    "assistant_delta",
    "thinking_delta",
    "llm_end",
    "assistant_message",
    "tool_start",
    "tool_decision",
    "tool_end",
    "retry",
    "compression",
    "stop",
    "error",
    "turn_end",
]

# High-frequency incremental events, only produced when a sink opts in via
# wants_deltas. Platform paths (no renderer) never receive these.
DELTA_EVENT_TYPES: frozenset[str] = frozenset({"assistant_delta", "thinking_delta"})


@dataclass(slots=True)
class ConversationEvent:
    type: ConversationEventType
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class ConversationEventSink:
    """Async event sink protocol implemented as a base class for convenience.

    ``wants_deltas`` gates the high-frequency assistant_delta/thinking_delta
    stream. It defaults to False so platform paths and recorders pay nothing;
    a live renderer sets it True to receive token-by-token updates.
    """

    wants_deltas: bool = False

    async def emit(self, event: ConversationEvent) -> None:
        raise NotImplementedError


class EventRecorder(ConversationEventSink):
    """Collect events while optionally forwarding them to another sink.

    Delta events are forwarded (if the downstream renderer wants them) but never
    stored — otherwise a long streamed turn would pile thousands of throwaway
    chunk objects into ``events``.
    """

    def __init__(self, forward: ConversationEventSink | None = None) -> None:
        self.events: list[ConversationEvent] = []
        self.forward = forward

    @property
    def wants_deltas(self) -> bool:
        return bool(self.forward is not None and getattr(self.forward, "wants_deltas", False))

    async def emit(self, event: ConversationEvent) -> None:
        if event.type not in DELTA_EVENT_TYPES:
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


async def emit_delta(
    sink: ConversationEventSink | None,
    event_type: ConversationEventType,
    chunk: str,
) -> None:
    """Emit a high-frequency delta event, but only if the sink opts in.

    Keeps platform paths (wants_deltas=False) free of per-token overhead.
    """
    if sink is None or not chunk:
        return
    if not getattr(sink, "wants_deltas", False):
        return
    await sink.emit(ConversationEvent(type=event_type, message="", data={"chunk": chunk}))
