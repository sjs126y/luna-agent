"""Renderer base: turn a ConversationEvent stream into semantic callbacks.

The inline TUI renderer subclasses this. The base only does
``event.type -> on_<type>`` dispatch; every callback defaults
to a no-op so a subclass overrides only the events it cares about. All gating /
drawing decisions belong to the subclass, not here — this keeps the dispatch
identical across renderers while leaving presentation fully to each one.
"""

from __future__ import annotations

from personal_agent.conversation.events import ConversationEvent, ConversationEventSink


class Renderer(ConversationEventSink):
    """Dispatch ConversationEvents to semantic ``on_*`` coroutine methods."""

    async def emit(self, event: ConversationEvent) -> None:
        handler = self._DISPATCH.get(event.type)
        if handler is None:
            return
        await getattr(self, handler)(event)

    # event.type -> method name. Keep in sync with ConversationEventType.
    _DISPATCH = {
        "turn_start": "on_turn_start",
        "llm_start": "on_llm_start",
        "assistant_delta": "on_assistant_delta",
        "thinking_delta": "on_thinking_delta",
        "llm_end": "on_llm_end",
        "assistant_message": "on_assistant_message",
        "tool_start": "on_tool_start",
        "tool_decision": "on_tool_decision",
        "tool_end": "on_tool_end",
        "artifact_available": "on_artifact_available",
        "response_artifact_selected": "on_response_artifact_selected",
        "retry": "on_retry",
        "compression": "on_compression",
        "steer_consumed": "on_steer_consumed",
        "stop": "on_stop",
        "error": "on_error",
        "turn_end": "on_turn_end",
    }

    async def on_turn_start(self, event: ConversationEvent) -> None: ...
    async def on_llm_start(self, event: ConversationEvent) -> None: ...
    async def on_assistant_delta(self, event: ConversationEvent) -> None: ...
    async def on_thinking_delta(self, event: ConversationEvent) -> None: ...
    async def on_llm_end(self, event: ConversationEvent) -> None: ...
    async def on_assistant_message(self, event: ConversationEvent) -> None: ...
    async def on_tool_start(self, event: ConversationEvent) -> None: ...
    async def on_tool_decision(self, event: ConversationEvent) -> None: ...
    async def on_tool_end(self, event: ConversationEvent) -> None: ...
    async def on_artifact_available(self, event: ConversationEvent) -> None: ...
    async def on_response_artifact_selected(self, event: ConversationEvent) -> None: ...
    async def on_retry(self, event: ConversationEvent) -> None: ...
    async def on_compression(self, event: ConversationEvent) -> None: ...
    async def on_steer_consumed(self, event: ConversationEvent) -> None: ...
    async def on_stop(self, event: ConversationEvent) -> None: ...
    async def on_error(self, event: ConversationEvent) -> None: ...
    async def on_turn_end(self, event: ConversationEvent) -> None: ...
