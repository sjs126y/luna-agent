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
        # Hint that the last tool's full output can be expanded with Ctrl+O.
        if state.last_expandable and not tools and not state.pending_confirm:
            lines.append(theme.sgr("  (Ctrl+O 展开上一个工具输出)", theme.TOOL_HINT))
        if state.pending_confirm:
            lines.append(theme.sgr(f"  ⚠ {state.pending_confirm}  [y/n/a]", theme.CONFIRM))
        return ANSI("\n".join(lines))

    def status_content() -> ANSI:
        return ANSI(_status_bar(state))

    def keyhint_content() -> ANSI:
        return ANSI(_keyhint_bar())

    active_window = Window(
        content=FormattedTextControl(active_content),
        height=Dimension(min=0, max=_MAX_ACTIVE_LINES, weight=1),
        wrap_lines=True,
    )
    status_window = Window(
        content=FormattedTextControl(status_content),
        height=1,
    )
    keyhint_window = Window(
        content=FormattedTextControl(keyhint_content),
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

    # Status + key hints now live BELOW the input box (per user preference),
    # so the eye reads: live output → your input → context/mode → key hints.
    body = HSplit([
        ConditionalContainer(active_window, filter=Condition(state.has_active_region)),
        input_area,
        status_window,
        keyhint_window,
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
    # Info line (below input): mode in its own color · model · context meter.
    mode = theme.sgr(f"● {state.exec_mode}", theme.mode_style(state.exec_mode))
    parts: list[str] = [mode, theme.dim(state.model or "-")]
    if state.context_window:
        used = state.input_tokens + state.output_tokens
        frac = used / state.context_window if state.context_window else 0.0
        pct = int(frac * 100)
        meter = theme.sgr(theme.spark_meter(frac), theme.STATUS_ACCENT)
        usage = (
            f"{theme.humanize(used)}/{theme.humanize(state.context_window)}"
        )
        parts.append(f"{meter} {theme.dim(usage)} {theme.sgr(f'{pct}%', theme.STATUS_ACCENT)}")
    return theme.dim("   ").join(parts)


def _keyhint_bar() -> str:
    # Key hints line (below the info line). Keys highlighted, labels dim.
    def key(k: str, label: str) -> str:
        return theme.sgr(k, theme.STATUS_ACCENT) + theme.dim(f" {label}")

    hints = [
        key("⏎", "发送"),
        key("Ctrl+J", "换行"),
        key("Ctrl+O", "展开"),
        key("Ctrl+C", "停止"),
        key("⇧Tab", "模式"),
        key("/", "命令"),
    ]
    return theme.dim("  ").join(hints)
