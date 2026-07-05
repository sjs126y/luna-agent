"""Per-turn agent execution report."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from time import monotonic
from typing import Any, Literal

from personal_agent.conversation.events import ConversationEvent, ConversationEventSink

TurnStatus = Literal["running", "completed", "failed", "stopped", "context_overflow"]

_SUMMARY_LIMIT = 500


@dataclass
class TurnLlmReport:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_call_count: int = 0
    model: str = ""
    context_window: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TurnToolReport:
    tool_name: str
    tool_use_id: str
    status: str = ""
    category: str = ""
    duration: float = 0.0
    input_summary: str = ""
    output_summary: str = ""
    error: str = ""
    decision_stage: str = ""
    permission_category: str = ""
    permission_decision: str = ""
    reason_code: str = ""
    required_allow: str = ""
    execution_mode: str = ""
    grant_matched: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TurnRetryReport:
    category: str = ""
    attempt: int = 0
    tool_name: str = ""
    error: str = ""
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentTurnReport:
    turn_id: str = ""
    status: TurnStatus = "running"
    completed: bool = False
    duration: float = 0.0
    error: str = ""
    user_message_summary: str = ""
    final_response_summary: str = ""
    initial_message_count: int = 0
    was_compressed: bool = False
    should_review_memory: bool = False
    llm: TurnLlmReport = field(default_factory=TurnLlmReport)
    retries: list[TurnRetryReport] = field(default_factory=list)
    event_counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._started = monotonic()
        self._tool_items: dict[str, TurnToolReport] = {}

    def apply_event(self, event: ConversationEvent) -> None:
        self.event_counts[event.type] = self.event_counts.get(event.type, 0) + 1
        data = event.data

        if event.type == "turn_start":
            self.turn_id = str(data.get("turn_id") or self.turn_id)
            self.user_message_summary = _summarize(data.get("user_message") or "")
            self.initial_message_count = _as_int(data.get("message_count"))
            self.was_compressed = bool(data.get("was_compressed", False))
        elif event.type == "llm_start":
            model = str(data.get("model") or "")
            if model:
                self.llm.model = model
        elif event.type == "llm_end":
            self.llm.calls += 1
            self.llm.input_tokens += _as_int(data.get("input_tokens"))
            self.llm.output_tokens += _as_int(data.get("output_tokens"))
            self.llm.tool_call_count += _as_int(data.get("tool_call_count"))
            model = str(data.get("model") or "")
            if model:
                self.llm.model = model
            context_window = _as_int(data.get("context_window"))
            if context_window:
                self.llm.context_window = context_window
        elif event.type == "tool_decision":
            self._apply_tool_decision(data)
        elif event.type == "tool_end":
            self._apply_tool_end(data)
        elif event.type == "retry":
            self.retries.append(TurnRetryReport(
                category=str(data.get("category") or ""),
                attempt=_as_int(data.get("attempt")),
                tool_name=str(data.get("tool_name") or ""),
                error=str(data.get("error") or ""),
                message=event.message,
            ))
        elif event.type == "assistant_message":
            self.final_response_summary = _summarize(event.message)
        elif event.type == "stop":
            self.status = "stopped"
            self.completed = False
        elif event.type == "error":
            self.status = "failed"
            self.completed = False
            self.error = str(data.get("error") or event.message or "")
        elif event.type == "turn_end":
            self._apply_turn_end(data)

    def finish(self, result: dict[str, Any]) -> dict[str, Any]:
        status = str(result.get("status") or "")
        completed = bool(result.get("completed", self.completed))
        if result.get("context_overflow"):
            self.status = "context_overflow"
        elif status == "stopped":
            self.status = "stopped"
        elif status == "failed":
            self.status = "failed"
        elif completed:
            self.status = "completed"
        elif self.status == "running":
            self.status = "failed"

        self.completed = completed
        self.error = str(result.get("error") or self.error or "")
        final_response = str(result.get("final_response") or "")
        if final_response:
            self.final_response_summary = _summarize(final_response)
        self.should_review_memory = bool(result.get("should_review_memory", self.should_review_memory))
        self.duration = max(monotonic() - self._started, 0.0)
        return result

    def as_dict(self) -> dict[str, Any]:
        tool_items = list(self._tool_items.values())
        status_counts = {
            "success": 0,
            "error": 0,
            "denied": 0,
            "timeout": 0,
            "interrupted": 0,
            "skipped": 0,
        }
        for item in tool_items:
            if item.status in status_counts:
                status_counts[item.status] += 1
        return {
            "turn_id": self.turn_id,
            "status": self.status,
            "completed": self.completed,
            "duration": self.duration,
            "error": self.error,
            "user_message_summary": self.user_message_summary,
            "final_response_summary": self.final_response_summary,
            "initial_message_count": self.initial_message_count,
            "was_compressed": self.was_compressed,
            "should_review_memory": self.should_review_memory,
            "llm": self.llm.as_dict(),
            "tools": {
                "total": len(tool_items),
                **status_counts,
                "items": [item.as_dict() for item in tool_items],
            },
            "retries": [retry.as_dict() for retry in self.retries],
            "event_counts": dict(sorted(self.event_counts.items())),
        }

    def _apply_tool_decision(self, data: dict[str, Any]) -> None:
        tool_use_id = str(data.get("tool_use_id") or data.get("tool_name") or "tool")
        item = self._tool(tool_use_id, str(data.get("tool_name") or ""))
        item.decision_stage = str(data.get("stage") or "")
        item.permission_category = str(data.get("permission_category") or "")
        item.permission_decision = str(data.get("permission_decision") or "")
        item.reason_code = str(data.get("reason_code") or "")
        item.required_allow = str(data.get("required_allow") or "")
        item.execution_mode = str(data.get("execution_mode") or "")
        item.grant_matched = str(data.get("grant_matched") or "")

    def _apply_tool_end(self, data: dict[str, Any]) -> None:
        tool_use_id = str(data.get("tool_use_id") or data.get("tool_name") or "tool")
        item = self._tool(tool_use_id, str(data.get("tool_name") or ""))
        item.status = str(data.get("status") or "")
        item.category = str(data.get("category") or "")
        item.duration = _as_float(data.get("duration"))
        item.input_summary = str(data.get("input_summary") or "")
        item.output_summary = str(data.get("output_summary") or "")
        item.error = str(data.get("error") or "")
        item.decision_stage = str(data.get("guard_stage") or item.decision_stage)
        item.permission_category = str(data.get("permission_category") or item.permission_category)
        item.permission_decision = str(data.get("permission_decision") or item.permission_decision)
        item.reason_code = str(data.get("guard_reason_code") or item.reason_code)
        item.required_allow = str(data.get("required_allow") or item.required_allow)
        item.execution_mode = str(data.get("execution_mode") or item.execution_mode)
        item.grant_matched = str(data.get("grant_matched") or item.grant_matched)

    def _apply_turn_end(self, data: dict[str, Any]) -> None:
        status = str(data.get("status") or "")
        if status in {"completed", "failed", "stopped", "context_overflow"}:
            self.status = status  # type: ignore[assignment]
        self.completed = bool(data.get("completed", self.completed))
        final_response = str(data.get("final_response") or "")
        if final_response:
            self.final_response_summary = _summarize(final_response)
        if "should_review_memory" in data:
            self.should_review_memory = bool(data.get("should_review_memory"))
        if data.get("context_overflow"):
            self.status = "context_overflow"

    def _tool(self, tool_use_id: str, tool_name: str) -> TurnToolReport:
        item = self._tool_items.get(tool_use_id)
        if item is None:
            item = TurnToolReport(tool_name=tool_name or tool_use_id, tool_use_id=tool_use_id)
            self._tool_items[tool_use_id] = item
        elif tool_name and not item.tool_name:
            item.tool_name = tool_name
        return item


class TurnReportRecorder(ConversationEventSink):
    def __init__(self, forward: ConversationEventSink | None = None) -> None:
        self.forward = forward
        self.report = AgentTurnReport()

    @property
    def wants_deltas(self) -> bool:
        return bool(self.forward is not None and getattr(self.forward, "wants_deltas", False))

    async def emit(self, event: ConversationEvent) -> None:
        self.report.apply_event(event)
        if self.forward is not None:
            await self.forward.emit(event)


def _summarize(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) <= _SUMMARY_LIMIT:
        return text
    return text[:_SUMMARY_LIMIT] + f"...({len(text) - _SUMMARY_LIMIT} more chars)"


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
