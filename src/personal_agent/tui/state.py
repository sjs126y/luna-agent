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
    affected_paths: tuple[str, ...] = ()
    command_preview: str = ""
    url_preview: str = ""
    host: str = ""
    cwd: str = ""
    timeout_seconds: float | None = None
    method: str = ""
    process_label: str = ""
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
class ConfirmAction:
    """One selectable action in the pending confirm panel."""

    id: str
    label: str
    result: str
    shortcut: str = ""
    is_default: bool = False


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
    process_label: str = ""
    affected_paths: tuple[str, ...] = ()
    default_action: str = "allow"  # allow | deny | none
    available_actions: tuple[str, ...] = ("allow_once", "allow_always", "deny")
    actions: tuple[ConfirmAction, ...] = ()
    selected_action: int = 0

    def __post_init__(self) -> None:
        if self.actions:
            return
        specs = {
            "allow_once": ("Allow once", "allow", "A"),
            "deny": ("Deny", "deny", "Esc"),
            "allow_always": ("Always", "always", "Shift+A"),
        }
        actions: list[ConfirmAction] = []
        for action_id in ("allow_once", "deny", "allow_always"):
            if action_id not in self.available_actions:
                continue
            label, result, shortcut = specs[action_id]
            is_default = (
                (self.default_action == "allow" and action_id == "allow_once")
                or (self.default_action == "deny" and action_id == "deny")
            )
            actions.append(ConfirmAction(action_id, label, result, shortcut, is_default))
        self.actions = tuple(actions)
        for index, action in enumerate(self.actions):
            if action.is_default:
                self.selected_action = index
                break


@dataclass(frozen=True)
class SlashMenuItem:
    """One slash-command candidate drawn by the inline TUI."""

    text: str
    description: str = ""
    display_text: str = ""


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
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cache_hit_rate: float | None = None
    activity_total: int = 0
    activity_attention: bool = False
    exec_mode: str = "Ask First"

    # last expandable (display_name, full_text) for Ctrl+O
    last_expandable: tuple[str, str] | None = None

    # pending inline tool confirmation. Holds display state; the app owns the
    # Future that resolves to allow / deny / always.
    pending_confirm: ConfirmPrompt | None = None

    # True while the input buffer is in slash-command mode. slash_items controls
    # whether a visible command menu is needed.
    slash_mode: bool = False
    slash_items: tuple[SlashMenuItem, ...] = ()
    slash_selected: int = 0
    slash_scroll: int = 0
    slash_empty_message: str = ""

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

    def has_slash_menu(self) -> bool:
        return self.slash_mode and (bool(self.slash_items) or bool(self.slash_empty_message))
