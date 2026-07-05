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

from prompt_toolkit.completion import Completer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import History
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.widgets import TextArea

from personal_agent.tui.state import UIState
from personal_agent.tui import theme


def _term_width(default: int = 80) -> int:
    """Live terminal width, so the divider/status bar track window resizes."""
    try:
        from prompt_toolkit.application.current import get_app

        return max(20, get_app().output.get_size().columns)
    except Exception:
        try:
            import shutil

            return max(20, shutil.get_terminal_size().columns)
        except Exception:
            return default


# Cap the bottom active region so a long stream / many live tools can't push the
# input box off-screen. Finalized content already lives in scrollback; the active
# region is only a live preview, so trimming it loses nothing permanent.
_MAX_ACTIVE_LINES = 12
_MAX_ACTIVE_TOOLS = 6
_STREAM_TAIL_CHARS = 2000


def build_layout(
    state: UIState,
    *,
    completer: Completer | None = None,
    history: History | None = None,
) -> tuple[FloatContainer, TextArea]:
    """Return (root_container, input_area). Caller owns key bindings."""

    def active_content() -> ANSI:
        lines: list[str] = [theme.divider(_term_width())]
        if state.thinking_chars:
            lines.append(theme.sgr(f"  💭 思考中… ({state.thinking_chars} 字)", theme.THINKING))
        tools = list(state.active_tools.values())
        for item in tools[:_MAX_ACTIVE_TOOLS]:
            lines.append(f"  {theme.sgr('⚙', theme.TOOL_ACTIVE)} {theme.sgr(item.display_name + '…', theme.TOOL_ACTIVE)}")
        if len(tools) > _MAX_ACTIVE_TOOLS:
            lines.append(theme.dim(f"  … 还有 {len(tools) - _MAX_ACTIVE_TOOLS} 个工具在运行"))
        if state.stream_text:
            cursor = theme.sgr("▌", theme.TOOL_ACTIVE) if state.streaming else ""
            # Only the tail matters as a live preview; the full reply finalizes
            # into scrollback via assistant_message.
            preview = state.stream_text[-_STREAM_TAIL_CHARS:]
            if len(state.stream_text) > _STREAM_TAIL_CHARS:
                preview = "…" + preview
            gutter = theme.gutter(theme.AGENT_BAR, "Agent", theme.AGENT)
            lines.append(f"{gutter}  {preview}{cursor}")
        if state.pending_confirm:
            lines.append(theme.sgr(f"  ⚠ {state.pending_confirm}  [y/n/a]", theme.CONFIRM))
        return ANSI("\n".join(lines))

    def status_content() -> ANSI:
        return ANSI(_status_bar(state))

    active_window = Window(
        content=FormattedTextControl(active_content),
        height=Dimension(min=0, max=_MAX_ACTIVE_LINES, weight=1),
        wrap_lines=True,
    )
    status_window = Window(
        content=FormattedTextControl(status_content),
        height=1,
    )
    # multiline=True so Ctrl+J can add real newlines; height grows with content
    # (1 line idle, up to 6). Enter is bound by the app to submit, not newline.
    # get_line_prefix aligns wrapped/continuation lines under the "› " prompt so
    # a Ctrl+J newline doesn't look mis-indented against the first line.
    def _line_prefix(line_number: int, wrap_count: int):
        return "  " if (line_number > 0 or wrap_count > 0) else ""

    input_area = TextArea(
        height=Dimension(min=1, max=6),
        prompt="› ",
        multiline=True,
        wrap_lines=True,
        completer=completer,
        complete_while_typing=True,  # slash menu pops as you type '/'
        history=history,
        get_line_prefix=_line_prefix,
    )

    body = HSplit([
        ConditionalContainer(active_window, filter=Condition(state.has_active_region)),
        status_window,
        input_area,
    ])
    # FloatContainer hosts the completion menu popup above the input line.
    root = FloatContainer(
        content=body,
        floats=[
            Float(
                xcursor=True,
                ycursor=True,
                content=CompletionsMenu(max_height=8, scroll_offset=1),
            ),
        ],
    )
    return root, input_area


def _status_bar(state: UIState) -> str:
    # Mode gets a colored accent; everything else stays dim so it recedes.
    mode = theme.sgr(state.exec_mode, theme.STATUS_ACCENT)
    parts: list[str] = [mode, theme.dim(state.model or "-")]
    if state.context_window:
        used = state.input_tokens + state.output_tokens
        frac = used / state.context_window if state.context_window else 0.0
        pct = int(frac * 100)
        meter = theme.sgr(theme.spark_meter(frac), theme.STATUS_ACCENT)
        parts.append(theme.dim(f"{used:,}/{state.context_window:,} ") + meter + theme.dim(f" {pct}%"))
    hint = theme.dim("⏎ 发送 · Ctrl+J 换行 · Ctrl+C 停止 · Shift+Tab 模式 · /help")
    return theme.dim("  ·  ").join(parts) + theme.dim("   ") + hint
