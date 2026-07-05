"""Bottom active region layout for the inline TUI.

Structure (validated in scripts/spike_inline.py, Phase 0):
    HSplit([
        ConditionalContainer(active_window),   # streaming reply + live tools
        meter_window,                          # model + context meter
        input_area,                            # active prompt
        hint_window,                           # mode + key hints
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
        lines: list[str] = []
        if state.thinking_chars:
            lines.append(theme.sgr(f"💭 思考中… ({state.thinking_chars} 字)", theme.THINKING))
        tools = list(state.active_tools.values())
        for item in tools[:_MAX_ACTIVE_TOOLS]:
            lines.append(f"{theme.sgr('⚙', theme.TOOL_ACTIVE)} {theme.sgr(item.display_name + '…', theme.TOOL_ACTIVE)}")
        if len(tools) > _MAX_ACTIVE_TOOLS:
            lines.append(theme.dim(f"… 还有 {len(tools) - _MAX_ACTIVE_TOOLS} 个工具在运行"))
        if state.stream_text:
            cursor = theme.sgr("▌", theme.AGENT_BAR) if state.streaming else ""
            # Only the tail matters as a live preview; the full reply finalizes
            # into scrollback via assistant_message.
            preview = state.stream_text[-_STREAM_TAIL_CHARS:]
            if len(state.stream_text) > _STREAM_TAIL_CHARS:
                preview = "…" + preview
            bar = theme.sgr(theme.BAR, theme.AGENT_BAR)
            lines.append(f"{bar} {preview}{cursor}")
        if state.pending_confirm:
            lines.append(theme.sgr(f"⚠ {state.pending_confirm}  [y/n/a]", theme.CONFIRM))
        return ANSI("\n".join(lines))

    def meter_content() -> ANSI:
        # Line ABOVE the input: model + context usage meter.
        return ANSI(_meter_bar(state))

    def hint_content() -> ANSI:
        # Line BELOW the input: current mode + key hints, hugging the input box.
        return ANSI(_hint_bar(state))

    active_window = Window(
        content=FormattedTextControl(active_content),
        height=Dimension(min=0, max=_MAX_ACTIVE_LINES),
        wrap_lines=True,
    )
    meter_window = Window(
        content=FormattedTextControl(meter_content),
        height=1,
    )
    hint_window = Window(
        content=FormattedTextControl(hint_content),
        height=1,
    )
    # multiline=True so Ctrl+J can add real newlines; height grows with content
    # (1 line idle, up to 6). Enter is bound by the app to submit, not newline.
    def _line_prefix(line_number: int, wrap_count: int):
        return "    " if (line_number > 0 or wrap_count > 0) else ""

    input_area = TextArea(
        height=Dimension(min=1, preferred=1, max=6, weight=0),
        prompt=ANSI(theme.sgr("  ❯ ", theme.PROMPT)),
        multiline=True,
        wrap_lines=True,
        completer=completer,
        complete_while_typing=True,  # slash menu pops as you type '/'
        history=history,
        get_line_prefix=_line_prefix,
    )

    # Reading order top→bottom: live output → model/context → input → mode/key
    # hints. There is deliberately no weighted spacer: in full_screen=False
    # mode it makes prompt_toolkit reserve a tall app region and visually splits
    # the input from its meter/hints.
    body = HSplit([
        ConditionalContainer(active_window, filter=Condition(state.has_active_region)),
        meter_window,
        input_area,
        hint_window,
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


def _meter_bar(state: UIState) -> str:
    # Line ABOVE the input box: model name + a colored context-usage meter.
    model = theme.sgr(state.model or "-", theme.METER_MODEL)
    if not state.context_window:
        return "  " + model
    used = state.input_tokens + state.output_tokens
    frac = used / state.context_window if state.context_window else 0.0
    pct = int(frac * 100)
    color = theme.meter_style(frac)
    meter = theme.sgr(theme.bar_meter(frac), color)
    usage = theme.dim(f"{theme.humanize(used)}/{theme.humanize(state.context_window)}")
    pct_s = theme.sgr(f"{pct}%", color)
    return f"  {model}  {meter}  {usage} {pct_s}"


def _hint_bar(state: UIState) -> str:
    # Line BELOW the input box: current mode (colored) + compact key hints.
    mode = theme.sgr(f"● {state.exec_mode}", theme.mode_style(state.exec_mode))

    def key(k: str, label: str) -> str:
        return theme.sgr(k, theme.KEY) + theme.sgr(label, theme.HINT_LABEL)

    hints = "  ".join([
        key("Enter", "发送"),
        key("Ctrl+J", "换行"),
        key("Ctrl+O", "展开"),
        key("Ctrl+C", "停止"),
        key("Shift+Tab", "模式"),
        key("/", "命令"),
    ])
    return f"  {mode}   {hints}"
