"""Bottom active region layout for the inline TUI.

Structure (validated in scripts/spike_inline.py, Phase 0):
    HSplit([
        ConditionalContainer(active_window),   # streaming reply + live tools
        status_window,                         # one-line status bar
        input_area,                            # pinned to bottom
    ])
The app is full_screen=False, so finalized content printed via run_in_terminal
lands ABOVE this region and enters native scrollback.
"""

from __future__ import annotations

from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import TextArea

from personal_agent.tui.state import UIState

_DIVIDER = "\x1b[2m" + "─" * 50 + "\x1b[0m"


def build_layout(state: UIState) -> tuple[HSplit, TextArea]:
    """Return (root_container, input_area). Caller owns key bindings."""

    def active_content() -> ANSI:
        lines: list[str] = [_DIVIDER]
        if state.thinking_chars:
            lines.append(f"\x1b[2m💭 思考中… ({state.thinking_chars} 字)\x1b[0m")
        for item in state.active_tools.values():
            lines.append(f"\x1b[36m⚙ {item.display_name}…\x1b[0m")
        if state.stream_text:
            cursor = "\x1b[36m▌\x1b[0m" if state.streaming else ""
            lines.append(f"\x1b[1;36mPersonal Agent:\x1b[0m {state.stream_text}{cursor}")
        return ANSI("\n".join(lines))

    def status_content() -> ANSI:
        return ANSI(_status_bar(state))

    active_window = Window(
        content=FormattedTextControl(active_content),
        height=Dimension(min=0, weight=1),
        wrap_lines=True,
    )
    status_window = Window(
        content=FormattedTextControl(status_content),
        height=1,
    )
    input_area = TextArea(height=1, prompt="› ", multiline=False, wrap_lines=False)

    root = HSplit([
        ConditionalContainer(active_window, filter=Condition(state.has_active_region)),
        status_window,
        input_area,
    ])
    return root, input_area


def _status_bar(state: UIState) -> str:
    parts: list[str] = [state.exec_mode, state.model or "-"]
    if state.context_window:
        used = state.input_tokens + state.output_tokens
        pct = int(used / state.context_window * 100) if state.context_window else 0
        parts.append(f"{used:,}/{state.context_window:,} ({pct}%)")
    parts.append("⏎ 发送 · Ctrl+J 换行 · Ctrl+C 停止 · /help")
    return "\x1b[2m" + "  ·  ".join(parts) + "\x1b[0m"
