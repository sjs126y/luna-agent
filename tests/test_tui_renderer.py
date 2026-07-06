"""Unit tests for the inline TUI renderer: event -> UIState mapping.

No real terminal needed — invalidate/print_above are captured as fakes and we
assert on UIState and the printed-to-scrollback lines.
"""

from __future__ import annotations

import pytest

from personal_agent.conversation.events import ConversationEvent
from personal_agent.tui.renderer import InlineRenderer
from personal_agent.tui.renderer_base import Renderer
from personal_agent.tui.state import UIState


def _make():
    printed: list[str] = []
    calls = {"n": 0}

    def invalidate() -> None:
        calls["n"] += 1

    async def print_above(text: str) -> None:
        printed.append(text)

    r = InlineRenderer(
        invalidate=invalidate,
        print_above=print_above,
        width=60,
    )
    return r, printed, calls


@pytest.mark.asyncio
async def test_streaming_accumulates_and_finalizes():
    r, printed, _ = _make()
    await r.emit(ConversationEvent("turn_start"))
    await r.emit(ConversationEvent("assistant_delta", data={"chunk": "Hel"}))
    await r.emit(ConversationEvent("assistant_delta", data={"chunk": "lo"}))
    assert r.state.stream_text == "Hello"
    assert r.state.streaming is True

    await r.emit(ConversationEvent("assistant_message", message="# Done"))
    # finalized: streaming cleared, reply pushed to scrollback
    assert r.state.stream_text == ""
    assert r.state.streaming is False
    assert len(printed) == 1
    assert "Done" in printed[0]


@pytest.mark.asyncio
async def test_llm_end_updates_status_fields():
    r, _, _ = _make()
    await r.emit(ConversationEvent("llm_end", data={
        "input_tokens": 10, "output_tokens": 5, "model": "deepseek-chat",
        "context_window": 128000,
        "context_used_tokens": 1200,
        "context_remaining_tokens": 126800,
        "context_percent": 0.9,
        "context_budget": {"used": 1200, "context_limit": 128000},
        "api_calls": 2,
        "cache_hit_tokens": 100,
        "cache_miss_tokens": 25,
        "cache_write_tokens": 12,
        "cache_read_tokens": 88,
        "cache_hit_rate": 0.8,
    }))
    assert r.state.model == "deepseek-chat"
    assert r.state.input_tokens == 10
    assert r.state.output_tokens == 5
    assert r.state.context_window == 128000
    assert r.state.context_used_tokens == 1200
    assert r.state.context_remaining_tokens == 126800
    assert r.state.context_percent == 0.9
    assert r.state.context_budget == {"used": 1200, "context_limit": 128000}
    assert r.state.api_calls == 2
    assert r.state.cache_hit_tokens == 100
    assert r.state.cache_miss_tokens == 25
    assert r.state.cache_write_tokens == 12
    assert r.state.cache_read_tokens == 88
    assert r.state.cache_hit_rate == 0.8


@pytest.mark.asyncio
async def test_tool_lifecycle_and_expandable():
    r, printed, _ = _make()
    await r.emit(ConversationEvent("tool_start", data={
        "tool_name": "read", "tool_use_id": "t1", "input_summary": "foo.py",
    }))
    assert "t1" in r.state.active_tools

    await r.emit(ConversationEvent("tool_end", data={
        "tool_use_id": "t1", "tool_name": "read", "status": "success",
        "duration": 0.3, "full_output": "DATA",
    }))
    assert "t1" not in r.state.active_tools
    assert r.state.last_expandable == ("read #1", "DATA")
    assert any("read" in line for line in printed)
    assert "Ctrl+O expand" not in "\n".join(printed)


@pytest.mark.asyncio
async def test_tool_end_hints_expand_for_long_output():
    r, printed, _ = _make()
    await r.emit(ConversationEvent("tool_end", data={
        "tool_use_id": "t1",
        "tool_name": "read",
        "display_name": "Read file",
        "status": "success",
        "input_preview": "large.log",
        "full_output": "line1\nline2\nline3\nline4",
    }))
    text = "\n".join(printed)
    assert "Read file" in text
    assert "large.log" in text
    assert "Ctrl+O expand" in text
    assert r.state.last_expandable == ("Read file #1", "line1\nline2\nline3\nline4")


@pytest.mark.asyncio
async def test_tool_decision_updates_mode_label():
    r, _, _ = _make()
    await r.emit(ConversationEvent("tool_start", data={
        "tool_name": "bash", "tool_use_id": "t1", "input_summary": "{}",
    }))
    await r.emit(ConversationEvent("tool_decision", data={
        "tool_name": "bash",
        "tool_use_id": "t1",
        "execution_mode_label": "Edit Freely",
        "display_name": "Shell command",
        "input_preview": "ls -la",
        "risk_level": "medium",
        "risk_summary": "Will execute a shell command.",
    }))
    assert r.state.exec_mode == "Edit Freely"
    item = r.state.active_tools["t1"]
    assert item.display_name == "Shell command"
    assert item.input_preview == "ls -la"
    assert item.risk_summary == "Will execute a shell command."


@pytest.mark.asyncio
async def test_tool_end_uses_display_metadata_in_trace():
    r, printed, _ = _make()
    await r.emit(ConversationEvent("tool_end", data={
        "tool_use_id": "t1",
        "tool_name": "bash",
        "display_name": "Shell command",
        "status": "denied",
        "input_preview": "rm -rf build",
        "risk_summary": "Will execute a shell command.",
        "error": "not allowed",
    }))
    text = "\n".join(printed)
    assert "Shell command" in text
    assert "rm -rf build" in text
    assert "Will execute a shell" in text
    assert "command." in text


@pytest.mark.asyncio
async def test_tool_end_summarizes_web_search_args_without_raw_json():
    r, printed, _ = _make()
    await r.emit(ConversationEvent("tool_end", data={
        "tool_use_id": "t1",
        "tool_name": "web_search",
        "display_name": "Web search",
        "status": "success",
        "input_summary": '{"max_results": 5, "query": "MCP 最新进展 2026"}',
        "duration": 0.4,
    }))
    text = "\n".join(printed)
    assert "Web search" in text
    assert "Query MCP 最新进展 2026" in text
    assert "5 results" in text
    assert '"max_results"' not in text
    assert '{"' not in text


@pytest.mark.asyncio
async def test_tool_end_shows_process_label():
    r, printed, _ = _make()
    await r.emit(ConversationEvent("tool_end", data={
        "tool_use_id": "t1",
        "tool_name": "process_start",
        "display_name": "Start process",
        "status": "success",
        "process_label": "vite dev server",
    }))
    text = "\n".join(printed)
    assert "Start process" in text
    assert "Process vite dev server" in text


@pytest.mark.asyncio
async def test_retry_compression_stop_and_error_are_printed():
    r, printed, _ = _make()
    await r.emit(ConversationEvent("retry", "模型空回复，准备重试", data={
        "category": "empty_response",
        "attempt": 1,
        "max_attempts": 2,
        "recoverable": True,
    }))
    await r.emit(ConversationEvent("compression", "历史消息已压缩", data={
        "pre_message_count": 12,
        "post_message_count": 5,
    }))
    await r.emit(ConversationEvent("stop", "已停止", data={
        "reason": "user",
        "stopped_tools": 2,
        "stopped_agents": 1,
    }))
    await r.emit(ConversationEvent("error", "模型调用失败", data={
        "error": "Timeout",
        "category": "llm",
        "recoverable": False,
        "detail_id": "err-1",
    }))

    text = "\n".join(printed)
    assert "模型空回复" in text
    assert "1/2" in text
    assert "历史消息已压缩" in text
    assert "12 -> 5" in text
    assert "已停止" in text
    assert "user" in text
    assert "tools 2" in text
    assert "agents 1" in text
    assert "模型调用失败" in text
    assert "Timeout" in text
    assert "llm" in text
    assert "不可恢复" in text
    assert "err-1" in text
    assert r.state.status_message == "error"


@pytest.mark.asyncio
async def test_turn_end_resets_status():
    r, _, _ = _make()
    await r.emit(ConversationEvent("turn_start"))
    await r.emit(ConversationEvent("assistant_delta", data={"chunk": "x"}))
    await r.emit(ConversationEvent("turn_end"))
    assert r.state.streaming is False
    assert r.state.status_message == "ready"


@pytest.mark.asyncio
async def test_invalidate_called_on_updates():
    r, _, calls = _make()
    await r.emit(ConversationEvent("assistant_delta", data={"chunk": "a"}))
    assert calls["n"] >= 1


@pytest.mark.asyncio
async def test_wants_deltas_true():
    r, _, _ = _make()
    assert r.wants_deltas is True


@pytest.mark.asyncio
async def test_base_dispatch_ignores_unknown_and_noops():
    # Base Renderer with no overrides: every event is a silent no-op.
    r = Renderer()
    await r.emit(ConversationEvent("assistant_delta", data={"chunk": "x"}))
    await r.emit(ConversationEvent("turn_end"))
    # unknown type is ignored, no exception
    await r.emit(ConversationEvent("does_not_exist"))  # type: ignore[arg-type]
