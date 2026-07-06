"""Bottom active region layout for the inline TUI.

Structure (validated in scripts/spike_inline.py, Phase 0):
    HSplit([
        ConditionalContainer(active_window),   # streaming reply + live tools
        meter_window,                          # model + context meter
        input_area,                            # active prompt
        slash_window,                          # reserved slash command area
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
    FloatContainer,
    HSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import TextArea

from personal_agent.tui.state import UIState
from personal_agent.tui import theme


# Cap the bottom active region so a long stream / many live tools can't push the
# input box off-screen. Finalized content already lives in scrollback; the active
# region is only a live preview, so trimming it loses nothing permanent.
_MAX_ACTIVE_LINES = 12
_MAX_ACTIVE_TOOLS = 6
_STREAM_TAIL_CHARS = 2000
_SLASH_MENU_LINES = 5
SLASH_MENU_VISIBLE_ITEMS = _SLASH_MENU_LINES - 1


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
            lines.extend(_confirm_lines(state.pending_confirm))
        return ANSI("\n".join(lines))

    def meter_content() -> ANSI:
        # Line ABOVE the input: model + context usage meter.
        return ANSI(_meter_bar(state))

    def hint_content() -> ANSI:
        # Line BELOW the input: current mode + key hints, hugging the input box.
        return ANSI(_hint_bar(state))

    def slash_content() -> ANSI:
        return ANSI(_slash_menu(state))

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
    slash_menu = Window(
        content=FormattedTextControl(slash_content),
        height=Dimension(min=1, max=_SLASH_MENU_LINES),
        style="bg:#202331",
        wrap_lines=False,
    )
    # multiline=True so Ctrl+J can add real newlines; height grows with content
    # (1 line idle, up to 6). Enter is bound by the app to submit, not newline.
    def _line_prefix(line_number: int, wrap_count: int):
        return "    " if (line_number > 0 or wrap_count > 0) else ""

    input_area = TextArea(
        height=Dimension(min=1, max=6),
        # NOTE: use a native formatted-text tuple, NOT ANSI(...). An ANSI prompt
        # object routes through a BeforeInput processor that fights the buffer's
        # own text/cursor rendering and made typed input vanish. A (style, text)
        # tuple is the supported way to color a TextArea prompt.
        prompt=[
            ("bg:#242837 #8ab4ff bold", " ▌ "),
            ("bg:#242837 #9cdcfe bold", "❯ "),
        ],
        multiline=True,
        wrap_lines=True,
        dont_extend_height=True,
        style="bg:#242837 #e6e6e6",
        completer=completer,
        # The visible slash menu is drawn by this TUI, not prompt_toolkit's
        # completion popup; keeping the popup closed prevents hidden completion
        # state from stealing Up/Down/Enter.
        complete_while_typing=False,
        history=history,
        get_line_prefix=_line_prefix,
    )

    # Reading order top→bottom: live output → model/context → input → slash menu
    # slot → mode/key hints. There is deliberately no weighted spacer: in full_screen=False
    # mode it makes prompt_toolkit reserve a tall app region and visually splits
    # the input from its meter/hints.
    body = HSplit([
        ConditionalContainer(active_window, filter=Condition(state.has_active_region)),
        meter_window,
        input_area,
        ConditionalContainer(slash_menu, filter=Condition(state.has_slash_menu)),
        ConditionalContainer(hint_window, filter=Condition(lambda: not state.slash_mode)),
    ])
    # FloatContainer hosts the completion menu popup above the input line.
    root = FloatContainer(
        content=body,
        floats=[],
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


def _slash_menu_header() -> str:
    return (
        "  "
        + theme.sgr("commands", theme.SLASH_BORDER)
        + theme.sgr("  type to filter · ↑/↓ selects · Enter applies", theme.SLASH_META)
    )


def _slash_menu(state: UIState) -> str:
    lines = [_slash_menu_header()]
    max_items = SLASH_MENU_VISIBLE_ITEMS
    count = len(state.slash_items)
    if count <= 0:
        return "\n".join(lines)
    selected = max(0, min(state.slash_selected, count - 1))
    offset = max(0, min(state.slash_scroll, max(0, count - max_items)))
    if selected < offset:
        offset = selected
    elif selected >= offset + max_items:
        offset = selected - max_items + 1

    for row, item in enumerate(state.slash_items[offset:offset + max_items]):
        index = offset + row
        marker = theme.sgr("›", theme.SLASH_MARK) if index == selected else theme.sgr(" ", theme.SLASH_BORDER)
        description = f"  {theme.sgr(item.description, theme.SLASH_META)}" if item.description else ""
        label = item.display_text or item.text
        lines.append(f"  {marker} {theme.sgr(label, theme.SLASH_ITEM)}{description}")
    return "\n".join(lines)


def _confirm_lines(confirm) -> list[str]:
    accent = theme.risk_style(confirm.risk_level)
    lines = [
        theme.sgr("╭─ confirm ─────────────────────────────────────────", theme.CONFIRM_BORDER),
        f"{theme.sgr('│ ', theme.CONFIRM_BORDER)}{theme.sgr(confirm.display_name, accent)}",
    ]
    meta = " · ".join(
        part for part in (
            confirm.permission_category,
            confirm.execution_mode,
            confirm.risk_level,
        ) if part
    )
    if meta:
        lines.append(f"{theme.sgr('│ ', theme.CONFIRM_BORDER)}{theme.sgr(meta, theme.CONFIRM_DIM)}")
    if confirm.risk_summary:
        lines.append(f"{theme.sgr('│ ', theme.CONFIRM_BORDER)}{theme.sgr('Risk ', accent)}{theme.sgr(confirm.risk_summary, theme.CONFIRM_TEXT)}")
    lines.extend(_confirm_detail_lines(confirm))
    actions = set(confirm.available_actions)
    if confirm.default_action == "allow" and "allow_once" in actions:
        default = "Enter allow once"
    elif confirm.default_action == "deny" and "deny" in actions:
        default = "Enter deny"
    elif "allow_once" in actions:
        default = "A allow once"
    else:
        default = ""
    action_parts: list[str] = []
    if default:
        action_parts.append(theme.sgr(default, theme.KEY))
    if "allow_always" in actions:
        action_parts.append(theme.sgr("Shift+A", theme.KEY) + theme.sgr(" always", theme.HINT_LABEL))
    if "deny" in actions:
        action_parts.append(theme.sgr("Esc", theme.KEY) + theme.sgr(" deny", theme.HINT_LABEL))
    if action_parts:
        lines.append(f"{theme.sgr('│ ', theme.CONFIRM_BORDER)}" + "   ".join(action_parts))
    lines.append(theme.sgr("╰───────────────────────────────────────────────────", theme.CONFIRM_BORDER))
    return lines


def _confirm_detail_lines(confirm) -> list[str]:
    lines: list[str] = []
    if confirm.command_preview:
        lines.append(_confirm_detail("Cmd", confirm.command_preview))
    if confirm.url_preview:
        target = confirm.url_preview
        if confirm.host and confirm.host not in target:
            target = f"{target} ({confirm.host})"
        lines.append(_confirm_detail("URL", target))
    if confirm.process_label:
        lines.append(_confirm_detail("Process", confirm.process_label))
    if confirm.affected_paths:
        paths = ", ".join(confirm.affected_paths[:3])
        if len(confirm.affected_paths) > 3:
            paths += f" +{len(confirm.affected_paths) - 3}"
        lines.append(_confirm_detail("Path", paths))
    if confirm.input_preview and not lines:
        lines.append(_confirm_detail("Input", confirm.input_preview))
    return lines


def _confirm_detail(label: str, value: str) -> str:
    return (
        theme.sgr("│ ", theme.CONFIRM_BORDER)
        + theme.sgr(f"{label} ", theme.CONFIRM_DIM)
        + theme.sgr(value, theme.CONFIRM_TEXT)
    )
