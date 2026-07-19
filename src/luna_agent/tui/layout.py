"""Bottom active region layout for the inline TUI.

Structure used by the production inline renderer:
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

from luna_agent.tui.state import UIState
from luna_agent.tui import theme


# Cap the bottom active region so a long stream / many live tools can't push the
# input box off-screen. Finalized content already lives in scrollback; the active
# region is only a live preview, so trimming it loses nothing permanent.
_MAX_ACTIVE_LINES = 12
_MAX_ACTIVE_TOOLS = 6
_STREAM_TAIL_CHARS = 2000
_SLASH_MENU_LINES = 5
_CONFIRM_DETAIL_LIMIT = 118
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
            if lines:
                lines.append("")
            lines.append(
                f"{theme.sgr('⚙', theme.TOOL_ACTIVE)} "
                f"{theme.sgr(item.display_name, theme.TOOL_ACTIVE_NAME)}"
                f"{theme.sgr('…', theme.TOOL_ACTIVE)}"
            )
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
        elif state.status_message in {"Press Ctrl+C again to exit", "cleared", "stop requested"}:
            lines.append(theme.sgr(f"  {state.status_message}", theme.NOTICE))
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
        if line_number > 0 or wrap_count > 0:
            return [
                ("bg:#242837 #8ab4ff bold", " ▌ "),
                ("bg:#242837 #9cdcfe bold", "  "),
            ]
        return ""

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
    context = _context_summary(state)
    turn = _turn_token_summary(state)
    parts = [model]
    if context:
        parts.extend(context)
    if turn:
        parts.append(turn)
    return "  " + "  ".join(parts)


def _context_summary(state: UIState) -> list[str]:
    if not state.context_window or not state.context_used_tokens:
        return []
    frac = state.context_used_tokens / state.context_window
    pct_value = state.context_percent if state.context_percent else frac * 100
    pct = int(pct_value)
    frac = max(0.0, min(1.0, frac))
    color = theme.meter_style(frac)
    meter = theme.sgr(theme.bar_meter(frac), color)
    usage = theme.dim(
        f"{theme.humanize(state.context_used_tokens)}/{theme.humanize(state.context_window)}"
    )
    pct_s = theme.sgr(f"{pct}%", color)
    return [meter, f"{usage} {pct_s}"]


def _turn_token_summary(state: UIState) -> str:
    if not state.input_tokens and not state.output_tokens:
        return ""
    return theme.dim(
        f"↓{theme.humanize(state.input_tokens)} | ↑{theme.humanize(state.output_tokens)}"
    )


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


def _slash_menu_header(state: UIState) -> str:
    count = len(state.slash_items)
    position = ""
    if count > SLASH_MENU_VISIBLE_ITEMS:
        selected = max(0, min(state.slash_selected, count - 1))
        position = f" · {selected + 1}/{count}"
    return (
        "  "
        + theme.sgr("commands", theme.SLASH_BORDER)
        + theme.sgr(f"  type to filter · ↑/↓ selects · Enter applies{position}", theme.SLASH_META)
    )


def _slash_menu(state: UIState) -> str:
    lines = [_slash_menu_header(state)]
    max_items = SLASH_MENU_VISIBLE_ITEMS
    count = len(state.slash_items)
    if count <= 0:
        if state.slash_empty_message:
            lines.append("    " + theme.sgr(state.slash_empty_message, theme.SLASH_EMPTY))
        return "\n".join(lines)
    selected = max(0, min(state.slash_selected, count - 1))
    offset = max(0, min(state.slash_scroll, max(0, count - max_items)))
    if selected < offset:
        offset = selected
    elif selected >= offset + max_items:
        offset = selected - max_items + 1

    for row, item in enumerate(state.slash_items[offset:offset + max_items]):
        index = offset + row
        selected_row = index == selected
        marker = theme.sgr("›", theme.SLASH_MARK) if selected_row else theme.sgr(" ", theme.SLASH_BORDER)
        description = f"  {theme.sgr(item.description, theme.SLASH_META)}" if item.description else ""
        label = item.display_text or item.text
        item_style = theme.SLASH_SELECTED if selected_row else theme.SLASH_ITEM
        lines.append(f"  {marker} {theme.sgr(label, item_style)}{description}")
    return "\n".join(lines)


def _confirm_lines(confirm) -> list[str]:
    accent = theme.risk_style(confirm.risk_level)
    lines = [
        theme.sgr("▌ confirm", theme.CONFIRM_BORDER)
        + " "
        + theme.sgr(confirm.display_name, accent),
    ]
    if confirm.risk_summary:
        lines.append(
            f"{theme.sgr('  Risk ', accent)}"
            f"{theme.sgr(confirm.risk_summary, theme.CONFIRM_TEXT)}"
        )
    lines.extend(_confirm_detail_lines(confirm))
    action_lines = _confirm_action_lines(confirm)
    if action_lines:
        lines.extend(action_lines)
        lines.append(theme.sgr("  Enter select · ↑/↓ · 1/2/3 quick", theme.CONFIRM_DIM))
    return lines


def _confirm_action_lines(confirm) -> list[str]:
    if not confirm.actions:
        return []
    lines: list[str] = []
    selected = max(0, min(confirm.selected_action, len(confirm.actions) - 1))
    for index, action in enumerate(confirm.actions):
        marker = "›" if index == selected else " "
        number = f"{index + 1}>"
        suffix = " *" if action.is_default else ""
        label = f"  {marker} {number} {action.label}{suffix}"
        style = theme.CONFIRM_ACTION_SELECTED if index == selected else theme.CONFIRM_ACTION
        lines.append(theme.sgr(label, style))
    return lines


def _confirm_detail_lines(confirm) -> list[str]:
    lines: list[str] = []
    if confirm.tool_approval_mode:
        lines.append(_confirm_detail("Approval", confirm.tool_approval_mode))
    if confirm.command_preview:
        lines.append(_confirm_detail("Cmd", confirm.command_preview))
    if confirm.url_preview:
        target = confirm.url_preview
        if confirm.host and confirm.host not in target:
            target = f"{target} ({confirm.host})"
        lines.append(_confirm_detail("URL", target))
    if confirm.process_label:
        lines.append(_confirm_detail("Process", confirm.process_label))
    if confirm.requested_resources:
        for resource in confirm.requested_resources[:3]:
            label = _resource_label(resource.kind)
            value = " ".join(part for part in (resource.access, resource.resource) if part)
            lines.append(_confirm_detail(label, value))
        if len(confirm.requested_resources) > 3:
            extra = len(confirm.requested_resources) - 3
            lines.append(_confirm_detail("Resource", f"+{extra} more"))
    elif confirm.affected_paths:
        paths = ", ".join(confirm.affected_paths[:3])
        if len(confirm.affected_paths) > 3:
            paths += f" +{len(confirm.affected_paths) - 3}"
        lines.append(_confirm_detail("Path", paths))
    if confirm.input_preview and not lines:
        lines.append(_confirm_detail("Input", confirm.input_preview))
    return lines


def _resource_label(kind: str) -> str:
    value = str(kind or "").lower()
    if value in {"file", "path", "filesystem", "directory"}:
        return "Path"
    if value in {"host", "network", "url"}:
        return "Host"
    if value in {"command", "process"}:
        return "Process"
    return "Resource"


def _confirm_detail(label: str, value: str) -> str:
    value = _clip_detail(value)
    return (
        theme.sgr(f"  {label} ", theme.CONFIRM_DIM)
        + theme.sgr(value, theme.CONFIRM_TEXT)
    )


def _clip_detail(value: str, limit: int = _CONFIRM_DETAIL_LIMIT) -> str:
    value = " ".join(str(value).split())
    if len(value) <= limit:
        return value
    return value[:max(0, limit - 1)] + "…"
