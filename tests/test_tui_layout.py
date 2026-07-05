"""Unit tests for the inline TUI layout: active-region height cap + truncation.

No real terminal needed — build_layout returns real prompt_toolkit containers,
and we read the active window's FormattedTextControl text() to assert on what
would be drawn.
"""

from __future__ import annotations

from prompt_toolkit.formatted_text import to_plain_text

from personal_agent.tui.layout import (
    _MAX_ACTIVE_TOOLS,
    _STREAM_TAIL_CHARS,
    build_layout,
)
from personal_agent.tui.state import ToolTrace, UIState


def _active_text(state: UIState) -> str:
    root, _ = build_layout(state)
    # The active region is the ConditionalContainer in the body HSplit; find it
    # rather than hard-coding an index (layout order may change).
    from prompt_toolkit.layout.containers import ConditionalContainer

    for child in root.content.children:
        if isinstance(child, ConditionalContainer):
            control = child.content.content
            return to_plain_text(control.text())
    raise AssertionError("no active region found")


def test_layout_keeps_input_panel_compact_without_spacer():
    from prompt_toolkit.layout.containers import ConditionalContainer, Window

    root, input_area = build_layout(UIState())
    children = list(root.content.children)
    assert len(children) == 4
    # The live active region is first. A weighted spacer before it would make
    # the full_screen=False application reserve a tall block and split the
    # prompt from its meter/hints.
    assert isinstance(children[0], ConditionalContainer)
    assert all(isinstance(child, (ConditionalContainer, Window)) for child in children)
    height = input_area.window.height
    assert height.preferred == 1
    assert height.weight == 0


def test_active_region_truncates_many_tools():
    state = UIState()
    for i in range(_MAX_ACTIVE_TOOLS + 4):
        state.active_tools[f"t{i}"] = ToolTrace(
            index=i, tool_use_id=f"t{i}", name="read", display_name="read"
        )
    text = _active_text(state)
    # only the cap is shown as individual lines + a "还有 N 个" summary
    assert text.count("⚙ read…") == _MAX_ACTIVE_TOOLS
    assert "还有 4 个工具在运行" in text


def test_active_region_truncates_long_stream():
    state = UIState()
    state.stream_text = "x" * (_STREAM_TAIL_CHARS + 500)
    state.streaming = True
    text = _active_text(state)
    # leading ellipsis marks the trimmed head; only the tail is kept
    assert "…" in text
    # the shown x-run is capped near the tail length, not the full 2500
    assert text.count("x") <= _STREAM_TAIL_CHARS


def test_active_region_short_stream_not_truncated():
    state = UIState()
    state.stream_text = "hello world"
    text = _active_text(state)
    assert "hello world" in text
    assert "…" not in text


def test_active_region_shows_pending_confirm():
    state = UIState()
    state.pending_confirm = "允许执行 write_file?"
    assert state.has_active_region() is True
    text = _active_text(state)
    assert "允许执行 write_file?" in text
    assert "[y/n/a]" in text


def test_hint_bar_shows_expand_key():
    from personal_agent.tui.layout import _hint_bar

    bar = _hint_bar(UIState())
    assert "展开" in bar  # Ctrl+O expand is always advertised in the hint bar


def test_meter_bar_shows_model_and_usage():
    from personal_agent.tui.layout import _meter_bar

    state = UIState()
    state.model = "deepseek-v4-flash"
    state.context_window = 1_000_000
    state.input_tokens = 213
    bar = _meter_bar(state)
    assert "deepseek-v4-flash" in bar
    assert "213/1M" in bar
    assert "0%" in bar


def test_hint_bar_uses_distinct_mode_colors():
    from personal_agent.tui import theme
    from personal_agent.tui.layout import _hint_bar

    seen = set()
    for mode in ("Read Only", "Ask First", "Edit Freely", "Full Auto"):
        state = UIState()
        state.exec_mode = mode
        bar = _hint_bar(state)
        assert mode in bar
        code = theme.mode_style(mode)
        assert f"\x1b[{code}m" in bar
        seen.add(code)
    assert len(seen) == 4  # four distinct colors


def test_keyhint_bar_below_input_lists_shortcuts():
    from personal_agent.tui.layout import _hint_bar

    bar = _hint_bar(UIState())
    for token in (
        "Enter",
        "Ctrl+J",
        "Ctrl+O",
        "Ctrl+C",
        "Shift+Tab",
        "/",
        "发送",
        "换行",
        "展开",
        "停止",
        "模式",
        "命令",
    ):
        assert token in bar


def test_humanize_compacts_counts():
    from personal_agent.tui import theme

    assert theme.humanize(999) == "999"
    assert theme.humanize(1659) == "1.7k"
    assert theme.humanize(1_000_000) == "1M"
