"""Unit tests for the inline TUI layout: active-region height cap + truncation.

No real terminal needed — build_layout returns real prompt_toolkit containers,
and we read the active window's FormattedTextControl text() to assert on what
would be drawn.
"""

from __future__ import annotations

from prompt_toolkit.formatted_text import to_plain_text

from personal_agent.tui.layout import (
    _MAX_ACTIVE_TOOLS,
    _SLASH_MENU_LINES,
    _STREAM_TAIL_CHARS,
    build_layout,
)
from personal_agent.tui.state import ConfirmPrompt, ToolTrace, UIState


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


def _layout_children(state: UIState):
    root, input_area = build_layout(state)
    return root, input_area, list(root.content.children)


def test_layout_keeps_input_panel_compact_without_spacer():
    from prompt_toolkit.layout.containers import ConditionalContainer, Window

    _, input_area, children = _layout_children(UIState())
    assert len(children) == 5
    # The live active region is first. A weighted spacer before it would make
    # the full_screen=False application reserve a tall block and split the
    # prompt from its meter/hints.
    assert isinstance(children[0], ConditionalContainer)
    assert all(isinstance(child, (ConditionalContainer, Window)) for child in children)
    height = input_area.window.height
    # Input grows from 1 line up to 6. It must NOT pin preferred=1/weight=0:
    # that combination starved the buffer and made typed text disappear.
    assert height.min == 1
    assert height.max == 6
    assert input_area.window.dont_extend_height()
    assert "bg:#242837" in input_area.window.style


def test_slash_command_slot_is_conditional_and_fixed_height():
    from prompt_toolkit.layout.containers import ConditionalContainer

    state = UIState()
    state.slash_mode = True
    _, _, children = _layout_children(state)
    slash_slot = children[3]
    assert isinstance(slash_slot, ConditionalContainer)
    assert bool(slash_slot.filter()) is True
    assert slash_slot.content.height.min == _SLASH_MENU_LINES
    assert slash_slot.content.height.max == _SLASH_MENU_LINES

    state.slash_mode = False
    assert bool(slash_slot.filter()) is False


def test_slash_command_slot_shows_command_header():
    state = UIState(slash_mode=True)
    _, _, children = _layout_children(state)
    slash_slot = children[3]
    header_window = slash_slot.content.children[0]
    text = to_plain_text(header_window.content.text())
    assert "commands" in text
    assert "继续输入过滤" in text


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
    state.pending_confirm = ConfirmPrompt(
        title="需要确认",
        display_name="write_file",
        permission_category="write",
        risk_level="medium",
        risk_summary="将写入文件",
        input_preview="src/app.py",
    )
    assert state.has_active_region() is True
    text = _active_text(state)
    assert "需要确认" in text
    assert "write_file" in text
    assert "权限 write" in text
    assert "风险: 将写入文件" in text
    assert "Enter 允许本次" in text
    assert "Shift+A 始终允许" in text
    assert "Esc/Ctrl+C 拒绝" in text


def test_active_region_confirm_none_default_requires_explicit_allow():
    state = UIState()
    state.pending_confirm = ConfirmPrompt(
        title="需要确认",
        display_name="bash",
        default_action="none",
    )
    text = _active_text(state)
    assert "A 允许本次" in text
    assert "Enter 允许本次" not in text


def test_active_region_confirm_hides_unavailable_actions():
    state = UIState()
    state.pending_confirm = ConfirmPrompt(
        title="需要确认",
        display_name="bash",
        default_action="deny",
        available_actions=("deny",),
    )
    text = _active_text(state)
    assert "Enter 拒绝" in text
    assert "始终允许" not in text
    assert "A 允许本次" not in text


def test_active_region_confirm_shows_structured_details():
    state = UIState()
    state.pending_confirm = ConfirmPrompt(
        title="需要确认",
        display_name="Fetch URL",
        url_preview="https://example.test/a",
        host="example.test",
        affected_paths=("src/a.py", "src/b.py"),
    )
    text = _active_text(state)
    assert "网络: https://example.test/a" in text
    assert "路径: src/a.py, src/b.py" in text


def test_active_region_confirm_shows_command_detail():
    state = UIState()
    state.pending_confirm = ConfirmPrompt(
        title="需要确认",
        display_name="Shell command",
        command_preview="uv run pytest -q",
    )
    text = _active_text(state)
    assert "命令: uv run pytest -q" in text


def test_active_region_confirm_shows_process_label():
    state = UIState()
    state.pending_confirm = ConfirmPrompt(
        title="需要确认",
        display_name="Start process",
        process_label="vite dev server",
    )
    text = _active_text(state)
    assert "进程: vite dev server" in text


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


def test_input_buffer_roundtrips_typed_text():
    # Regression: an ANSI(...) prompt object routed through a BeforeInput
    # processor and made typed input disappear in a real terminal. The unit
    # layer can't render a TTY, but it can assert the buffer accepts text and
    # the layout builds with the native-tuple prompt.
    _, input_area = build_layout(UIState())
    input_area.text = "hello 世界"
    assert input_area.buffer.text == "hello 世界"
