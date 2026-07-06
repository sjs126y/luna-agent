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
        "context_window": 128000, "api_calls": 2,
    }))
    assert r.state.model == "deepseek-chat"
    assert r.state.input_tokens == 10
    assert r.state.output_tokens == 5
    assert r.state.context_window == 128000
    assert r.state.api_calls == 2


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
    assert r.state.last_expandable == ("read", "DATA")
    assert any("read" in line for line in printed)


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
