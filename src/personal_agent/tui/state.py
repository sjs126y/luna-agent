"""Observable UI state for the inline TUI.

InlineRenderer mutates this; InlineTuiApp reads it during redraw. Keeping all
mutable render state in one place (rather than scattered across the renderer)
makes the "renderer only touches state, never the terminal" rule easy to hold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time


@dataclass
class ToolTrace:
    """One tool call shown in the active region / finalized into scrollback."""

    index: int
    tool_use_id: str
    name: str
    display_name: str
    input_summary: str = ""
    input_preview: str = ""
    risk_level: str = ""
    risk_summary: str = ""
    started_at: float = 0.0
    status: str = "running"       # running | success | error | denied | ...
    output_summary: str = ""
    full_output: str = ""
    error: str = ""
    duration: float = 0.0

    def finish(self, *, status: str, output_summary: str, full_output: str,
               error: str, duration: float) -> None:
        self.status = status
        self.output_summary = output_summary
        self.full_output = full_output
        self.error = error
        self.duration = duration if duration > 0 else (
            max(0.0, time.monotonic() - self.started_at) if self.started_at else 0.0
        )


@dataclass
class ConfirmPrompt:
    """Human-facing state for one pending tool confirmation."""

    title: str
    display_name: str
    permission_category: str = ""
    execution_mode: str = ""
    risk_level: str = ""
    risk_summary: str = ""
    input_preview: str = ""
    command_preview: str = ""
    url_preview: str = ""
    host: str = ""
    affected_paths: tuple[str, ...] = ()
    default_action: str = "allow"  # allow | deny | none
    available_actions: tuple[str, ...] = ("allow_once", "allow_always", "deny")


@dataclass
class UIState:
    """Everything the bottom active region needs to draw the current turn."""

    # streaming reply (repainted in place while streaming)
    stream_text: str = ""
    thinking_chars: int = 0
    streaming: bool = False

    # spinner / status line
    status_message: str = "ready"

    # in-flight + completed tool traces for the current turn
    active_tools: dict[str, ToolTrace] = field(default_factory=dict)
    tool_seq: int = 0

    # status-bar fields (fed by llm_end)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    api_calls: int = 0
    context_window: int = 0
    exec_mode: str = "Ask First"

    # last expandable (display_name, full_text) for Ctrl+O
    last_expandable: tuple[str, str] | None = None

    # pending inline tool confirmation. Holds display state; the app owns the
    # Future that resolves to allow / deny / always.
    pending_confirm: ConfirmPrompt | None = None

    def reset_turn(self) -> None:
        self.stream_text = ""
        self.thinking_chars = 0
        self.streaming = False
        self.active_tools.clear()
        self.tool_seq = 0
        self.pending_confirm = None

    def has_active_region(self) -> bool:
        return bool(
            self.streaming
            or self.stream_text
            or self.active_tools
            or self.pending_confirm
        )
