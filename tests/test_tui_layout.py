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
    # body = HSplit([ConditionalContainer(active_window), status, input])
    active_window = root.content.children[0].content
    control = active_window.content
    return to_plain_text(control.text())


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


def test_active_region_shows_expand_hint_when_idle_with_expandable():
    state = UIState()
    state.last_expandable = ("read", "DATA")
    text = _active_text(state)
    assert "Ctrl+O 展开" in text


def test_status_bar_uses_distinct_mode_colors():
    from personal_agent.tui import theme
    from personal_agent.tui.layout import _status_bar

    seen = set()
    for mode in ("normal", "acceptEdits", "auto"):
        state = UIState()
        state.exec_mode = mode
        bar = _status_bar(state)
        assert mode in bar
        # the mode's color code appears in the raw (pre-plain) string
        code = theme.mode_style(mode)
        assert f"\x1b[{code}m" in bar
        seen.add(code)
    assert len(seen) == 3  # three distinct colors


def test_keyhint_bar_below_input_lists_shortcuts():
    from personal_agent.tui.layout import _keyhint_bar

    bar = _keyhint_bar()
    for token in ("发送", "换行", "展开", "停止", "模式", "命令"):
        assert token in bar


def test_humanize_compacts_counts():
    from personal_agent.tui import theme

    assert theme.humanize(999) == "999"
    assert theme.humanize(1659) == "1.7k"
    assert theme.humanize(1_000_000) == "1M"
