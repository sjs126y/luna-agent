"""Conversation event model and protocol metadata for frontends."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EVENT_PROTOCOL_VERSION = 1

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


@dataclass(frozen=True, slots=True)
class EventFieldSpec:
    name: str
    type: str
    description: str = ""
    required: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class EventSchema:
    type: str
    description: str
    message: str = "human-readable event summary"
    fields: tuple[EventFieldSpec, ...] = ()
    delta: bool = False

    def required_fields(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields if field.required)

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "description": self.description,
            "message": self.message,
            "delta": self.delta,
            "fields": [field.as_dict() for field in self.fields],
        }


EVENT_SCHEMAS: dict[str, EventSchema] = {
    "turn_start": EventSchema(
        "turn_start",
        "A user turn started.",
        fields=(
            EventFieldSpec("turn_id", "string", "Stable turn identifier when available."),
            EventFieldSpec("user_message", "string", "Raw user message text when available."),
            EventFieldSpec("message_count", "integer", "History message count before the turn."),
            EventFieldSpec("was_compressed", "boolean", "Whether history was compressed before the turn."),
        ),
    ),
    "llm_start": EventSchema(
        "llm_start",
        "An LLM request is starting.",
        fields=(
            EventFieldSpec("api_calls", "integer", "1-based API call count for the session."),
            EventFieldSpec("message_count", "integer", "Messages sent to the model."),
            EventFieldSpec("tool_count", "integer", "Tool definitions available to the model."),
            EventFieldSpec("model", "string", "Model name when known."),
        ),
    ),
    "assistant_delta": EventSchema(
        "assistant_delta",
        "Streaming assistant text delta. Only sent to sinks with wants_deltas=True.",
        fields=(EventFieldSpec("chunk", "string", "Text delta.", required=True),),
        delta=True,
    ),
    "thinking_delta": EventSchema(
        "thinking_delta",
        "Streaming reasoning/thinking delta. Only sent to sinks with wants_deltas=True.",
        fields=(EventFieldSpec("chunk", "string", "Thinking delta.", required=True),),
        delta=True,
    ),
    "llm_end": EventSchema(
        "llm_end",
        "An LLM response completed.",
        fields=(
            EventFieldSpec("input_tokens", "integer", "Input tokens reported by the provider."),
            EventFieldSpec("output_tokens", "integer", "Output tokens reported by the provider."),
            EventFieldSpec("tool_call_count", "integer", "Number of tool calls in the response."),
            EventFieldSpec("finish_reason", "string", "Provider finish reason."),
            EventFieldSpec("model", "string", "Resolved model name."),
            EventFieldSpec("context_window", "integer", "Model context window when known."),
        ),
    ),
    "assistant_message": EventSchema(
        "assistant_message",
        "A finalized assistant text message is available in event.message.",
    ),
    "tool_start": EventSchema(
        "tool_start",
        "A tool call started.",
        fields=(
            EventFieldSpec("tool_name", "string", "Tool name.", required=True),
            EventFieldSpec("tool_use_id", "string", "Provider/tool call id.", required=True),
            EventFieldSpec("input_summary", "string", "Compact input summary."),
        ),
    ),
    "tool_decision": EventSchema(
        "tool_decision",
        "The execution guard decided whether a tool may proceed.",
        fields=(
            EventFieldSpec("tool_name", "string", "Tool name.", required=True),
            EventFieldSpec("tool_use_id", "string", "Provider/tool call id.", required=True),
            EventFieldSpec("allowed", "boolean", "Whether execution is allowed."),
            EventFieldSpec("stage", "string", "Decision stage: lookup/precheck/permission/runtime_guard/execution."),
            EventFieldSpec("status", "string", "Decision status: allowed/denied/error."),
            EventFieldSpec("permission_category", "string", "Permission category such as write/bash/network."),
            EventFieldSpec("execution_mode", "string", "Execution policy profile."),
            EventFieldSpec("permission_decision", "string", "allow/ask/deny from execution policy."),
            EventFieldSpec("reason_code", "string", "Machine-readable denial/error reason."),
            EventFieldSpec("required_allow", "string", "Grant category that would allow this action."),
            EventFieldSpec("decision_message", "string", "Human-readable decision detail."),
            EventFieldSpec("grant_matched", "string", "Grant that matched, such as all/write."),
            EventFieldSpec("display_name", "string", "User-facing tool label."),
            EventFieldSpec("execution_mode_label", "string", "User-facing execution mode label."),
            EventFieldSpec("risk_level", "string", "low/medium/high display risk."),
            EventFieldSpec("risk_summary", "string", "Short user-facing risk explanation."),
            EventFieldSpec("default_action", "string", "allow/deny/none default confirmation action."),
            EventFieldSpec("available_actions", "list[string]", "Supported confirmation actions."),
            EventFieldSpec("input_summary", "string", "Compact redacted input summary."),
            EventFieldSpec("input_preview", "string", "Best redacted preview for confirmation UI."),
            EventFieldSpec("affected_paths", "list[string]", "Paths affected by file-oriented tools."),
            EventFieldSpec("command_preview", "string", "Command preview for shell/background tools."),
            EventFieldSpec("url_preview", "string", "URL preview for network tools."),
            EventFieldSpec("host", "string", "Parsed network host when available."),
            EventFieldSpec("cwd", "string", "Working directory preview for command tools."),
            EventFieldSpec("timeout_seconds", "number", "Configured timeout in seconds when available."),
            EventFieldSpec("method", "string", "HTTP method preview for network tools."),
            EventFieldSpec("process_label", "string", "User-facing label for background process tools."),
        ),
    ),
    "tool_end": EventSchema(
        "tool_end",
        "A tool call completed, failed, or was denied.",
        fields=(
            EventFieldSpec("tool_name", "string", "Tool name.", required=True),
            EventFieldSpec("tool_use_id", "string", "Provider/tool call id.", required=True),
            EventFieldSpec("status", "string", "success/error/denied/timeout/interrupted/skipped."),
            EventFieldSpec("category", "string", "Error or result category."),
            EventFieldSpec("error", "string", "Error text if any."),
            EventFieldSpec("duration", "number", "Duration in seconds."),
            EventFieldSpec("input_summary", "string", "Compact input summary."),
            EventFieldSpec("output_summary", "string", "Compact output summary."),
            EventFieldSpec("full_output", "string", "Full output or error for expansion/storage."),
            EventFieldSpec("output_truncated", "boolean", "Whether output was truncated."),
            EventFieldSpec("guard_stage", "string", "Guard stage from the related decision."),
            EventFieldSpec("guard_reason_code", "string", "Reason code from the related decision."),
            EventFieldSpec("permission_category", "string", "Permission category from the related decision."),
            EventFieldSpec("permission_decision", "string", "Policy decision from the related decision."),
            EventFieldSpec("required_allow", "string", "Required grant from the related decision."),
            EventFieldSpec("execution_mode", "string", "Execution mode from the related decision."),
            EventFieldSpec("grant_matched", "string", "Grant matched from the related decision."),
            EventFieldSpec("display_name", "string", "User-facing tool label."),
            EventFieldSpec("execution_mode_label", "string", "User-facing execution mode label."),
            EventFieldSpec("risk_level", "string", "low/medium/high display risk."),
            EventFieldSpec("risk_summary", "string", "Short user-facing risk explanation."),
            EventFieldSpec("default_action", "string", "allow/deny/none default confirmation action."),
            EventFieldSpec("available_actions", "list[string]", "Supported confirmation actions."),
            EventFieldSpec("input_preview", "string", "Best redacted preview for confirmation UI."),
            EventFieldSpec("affected_paths", "list[string]", "Paths affected by file-oriented tools."),
            EventFieldSpec("command_preview", "string", "Command preview for shell/background tools."),
            EventFieldSpec("url_preview", "string", "URL preview for network tools."),
            EventFieldSpec("host", "string", "Parsed network host when available."),
            EventFieldSpec("cwd", "string", "Working directory preview for command tools."),
            EventFieldSpec("timeout_seconds", "number", "Configured timeout in seconds when available."),
            EventFieldSpec("method", "string", "HTTP method preview for network tools."),
            EventFieldSpec("process_label", "string", "User-facing label for background process tools."),
        ),
    ),
    "retry": EventSchema(
        "retry",
        "The runtime is retrying or asking the model to recover.",
        fields=(
            EventFieldSpec("category", "string", "Retry category.", required=True),
            EventFieldSpec("attempt", "integer", "Retry attempt number when available."),
            EventFieldSpec("max_attempts", "integer", "Maximum attempts for this retry category when known."),
            EventFieldSpec("error", "string", "Error that caused retry when available."),
            EventFieldSpec("tool_name", "string", "Tool name for tool retry."),
            EventFieldSpec("tool_names", "string", "Comma-separated invalid tool names."),
            EventFieldSpec("recoverable", "boolean", "Whether the runtime expects automatic recovery."),
        ),
    ),
    "compression": EventSchema(
        "compression",
        "Conversation history was compressed.",
        fields=(
            EventFieldSpec("pre_message_count", "integer", "Message count before compression."),
            EventFieldSpec("post_message_count", "integer", "Message count after compression."),
        ),
    ),
    "stop": EventSchema(
        "stop",
        "The current turn was stopped/interrupted.",
        fields=(
            EventFieldSpec("reason", "string", "user/interrupt/timeout/shutdown when known."),
            EventFieldSpec("message", "string", "User-facing stop summary."),
            EventFieldSpec("stopped_tools", "integer", "Number of tool executions stopped when known."),
            EventFieldSpec("stopped_agents", "integer", "Number of delegated agents stopped when known."),
        ),
    ),
    "error": EventSchema(
        "error",
        "A runtime error occurred.",
        fields=(
            EventFieldSpec("error", "string", "Error text.", required=True),
            EventFieldSpec("category", "string", "Error category such as llm/runtime/tool."),
            EventFieldSpec("recoverable", "boolean", "Whether the runtime can continue automatically."),
            EventFieldSpec("detail_id", "string", "Stable detail/log id when available."),
        ),
    ),
    "turn_end": EventSchema(
        "turn_end",
        "A turn ended or was saved.",
        fields=(
            EventFieldSpec("session_key", "string", "Session key when emitted by the conversation service."),
            EventFieldSpec("status", "string", "completed/failed/stopped/context_overflow."),
            EventFieldSpec("completed", "boolean", "Whether the turn completed normally."),
            EventFieldSpec("final_response", "string", "Final assistant response when emitted by the agent loop."),
            EventFieldSpec("api_calls", "integer", "Session API call count."),
            EventFieldSpec("should_review_memory", "boolean", "Whether memory review should run."),
            EventFieldSpec("was_compressed", "boolean", "Whether the saved turn used compression."),
            EventFieldSpec("context_overflow", "boolean", "Whether the turn hit context overflow."),
        ),
    ),
}


@dataclass(slots=True)
class ConversationEvent:
    type: ConversationEventType
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": EVENT_PROTOCOL_VERSION,
            "type": self.type,
            "message": self.message,
            "data": dict(self.data),
        }


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


def event_protocol_schema() -> dict[str, Any]:
    """Return the frontend-facing event protocol contract."""
    return {
        "protocol_version": EVENT_PROTOCOL_VERSION,
        "delta_event_types": sorted(DELTA_EVENT_TYPES),
        "events": {name: schema.as_dict() for name, schema in EVENT_SCHEMAS.items()},
    }


def frontend_protocol_schema() -> dict[str, Any]:
    """Return the stable protocol contract consumed by TUI/desktop/web frontends."""
    return event_protocol_schema()


def validate_event_contract(event: ConversationEvent) -> list[str]:
    """Lightweight contract validation for tests and future debug tooling."""
    errors: list[str] = []
    schema = EVENT_SCHEMAS.get(event.type)
    if schema is None:
        return [f"unknown event type: {event.type}"]
    for field_name in schema.required_fields():
        if field_name not in event.data:
            errors.append(f"{event.type}.{field_name} is required")
    return errors
