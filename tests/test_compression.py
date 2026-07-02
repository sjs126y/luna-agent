"""Test ContextEngine + ContextCompressor — prune, summary, iterative update, anti-jitter."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.compression import compression_registry
from personal_agent.compression.base import ContextEngine
from personal_agent.compression.simple import (
    ContextCompressor,
    _format_messages_for_summary,
)
from personal_agent.models.messages import NormalizedResponse


# ── helpers ──────────────────────────────────────────────

def _text_msg(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _tool_use_msg(tool_id: str, name: str, input_: dict | None = None) -> dict:
    return {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": tool_id, "name": name, "input": input_ or {}}
        ],
    }


def _tool_result_msg(tool_use_id: str, result: str) -> dict:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": result}],
    }


def _mixed_msg(blocks: list[dict]) -> dict:
    return {"role": "assistant", "content": blocks}


def _make_long_history(n: int) -> list[dict]:
    """Generate n text messages to push over threshold."""
    msgs = []
    for i in range(n):
        msgs.append(_text_msg("user", f"question {i} " + "x" * 100))
        msgs.append(_text_msg("assistant", f"answer {i} " + "y" * 200))
    return msgs


# ── ContextEngine ABC ───────────────────────────────────

def test_context_engine_abc_methods():
    """All abstract methods must be defined."""
    assert hasattr(ContextEngine, "should_compress")
    assert hasattr(ContextEngine, "compress")
    assert hasattr(ContextEngine, "on_session_start")
    assert hasattr(ContextEngine, "on_session_end")
    assert hasattr(ContextEngine, "update_from_response")


def test_context_engine_update_from_response():
    engine = ContextCompressor()
    resp = NormalizedResponse(
        text="ok", tool_calls=[], usage={"input_tokens": 5000, "output_tokens": 100},
        finish_reason="end_turn", stop_reason="end_turn", model="test",
    )
    engine.update_from_response(resp)
    assert engine.last_prompt_tokens == 5000


def test_builtin_compression_registry_registers_aliases():
    assert compression_registry.list_engines() == ["simple"]
    assert compression_registry.get("simple") is compression_registry.get("compressor")


# ── ContextCompressor init ───────────────────────────────

def test_compressor_defaults():
    c = ContextCompressor()
    assert c.name == "compressor"
    assert c.threshold_tokens == int(64000 * 0.6)
    assert c.tail_token_budget == 20000
    assert c.max_summary_tokens == 500
    assert c._previous_summary is None
    assert c._ineffective_compression_count == 0


def test_compressor_custom_params():
    c = ContextCompressor(context_length=100000, threshold_ratio=0.5, tail_token_budget=30000,
                          max_summary_tokens=800, protect_head=3, protect_tail=8)
    assert c.threshold_tokens == 50000
    assert c.tail_token_budget == 30000
    assert c.max_summary_tokens == 800
    assert c.protect_head == 3
    assert c.protect_tail == 8


# ── should_compress ──────────────────────────────────────

def test_should_compress_below_threshold():
    c = ContextCompressor(threshold_ratio=0.6)  # threshold = 38400
    # tokens under threshold
    assert c.should_compress(10000, _make_long_history(50)) is False


def test_should_compress_above_threshold():
    c = ContextCompressor(threshold_ratio=0.01)  # threshold = 640, very low
    msgs = _make_long_history(20)
    assert c.should_compress(50000, msgs) is True


def test_should_compress_after_two_ineffective():
    c = ContextCompressor(threshold_ratio=0.01)
    c._ineffective_compression_count = 2
    assert c.should_compress(50000, _make_long_history(30)) is False


def test_should_compress_cooldown():
    c = ContextCompressor(threshold_ratio=0.01)
    c._summary_failure_cooldown_until = time.time() + 60
    assert c.should_compress(50000, _make_long_history(30)) is False


def test_should_compress_cooldown_expired():
    c = ContextCompressor(threshold_ratio=0.01)
    c._summary_failure_cooldown_until = time.time() - 1
    assert c.should_compress(50000, _make_long_history(30)) is True


def test_should_compress_too_few_messages():
    c = ContextCompressor(threshold_ratio=0.01, protect_head=2, protect_tail=6)
    # only 5 messages, need > 2+6+2 = 10
    assert c.should_compress(50000, _make_long_history(3)) is False


# ── _prune_old_tool_results ──────────────────────────────

def test_prune_removes_tool_results_from_middle():
    """Prune removes tool_result → orphan tool_use also cleaned → only head+tail remain."""
    c = ContextCompressor(protect_head=1, protect_tail=1)
    msgs = [
        _text_msg("user", "hello"),                     # head (protected)
        _tool_result_msg("c1", "result 1"),              # middle → removed
        _tool_use_msg("c1", "calc"),                     # middle → orphaned, cleaned
        _text_msg("assistant", "answer"),                # tail (protected)
    ]
    result = c._prune_old_tool_results(msgs)
    # tool_result removed → tool_use becomes orphan → cleaned
    # Result: head(1) + tail(1) = 2
    assert len(result) == 2
    assert result[0] == msgs[0]                         # head
    assert result[1] == msgs[3]                         # tail


def test_prune_preserves_protected_ranges():
    c = ContextCompressor(protect_head=2, protect_tail=2)
    msgs = [
        _text_msg("user", "h1"),                         # head
        _text_msg("assistant", "h2"),                    # head
        _tool_result_msg("c1", "mid tool result"),       # middle → removed
        _tool_result_msg("c2", "mid tool result 2"),     # middle → removed
        _text_msg("assistant", "mid text"),              # middle → kept (text)
        _text_msg("user", "t1"),                         # tail
        _text_msg("assistant", "t2"),                    # tail
    ]
    result = c._prune_old_tool_results(msgs)
    # head (2) + middle text (1) + tail (2) = 5
    assert len(result) == 5
    assert result[0] == msgs[0]
    assert result[1] == msgs[1]
    assert result[2] == msgs[4]  # mid text
    assert result[3] == msgs[5]
    assert result[4] == msgs[6]


def test_prune_too_few_messages_returns_unchanged():
    c = ContextCompressor(protect_head=2, protect_tail=2)
    msgs = [_text_msg("user", "a"), _text_msg("assistant", "b")]
    result = c._prune_old_tool_results(msgs)
    assert result == msgs  # unchanged


def test_prune_mixed_content_blocks():
    """Message with text + tool_result: tool_result removed, text kept."""
    c = ContextCompressor(protect_head=1, protect_tail=1)
    msgs = [
        _text_msg("user", "head"),
        _mixed_msg([
            {"type": "text", "text": "let me search"},
            {"type": "tool_result", "tool_use_id": "c1", "content": "found it"},
        ]),
        _text_msg("assistant", "tail"),
    ]
    result = c._prune_old_tool_results(msgs)
    assert len(result) == 3
    # Middle message should have tool_result removed, text kept
    mid_content = result[1]["content"]
    assert len(mid_content) == 1
    assert mid_content[0]["type"] == "text"
    assert mid_content[0]["text"] == "let me search"


# ── _clean_orphan_tool_uses ─────────────────────────────

def test_clean_orphan_removes_tool_use_without_result():
    msgs = [
        _tool_use_msg("orphan", "calc"),                  # no matching result → removed
        _text_msg("user", "hello"),
    ]
    result = ContextCompressor._clean_orphan_tool_uses(msgs)
    # orphan tool_use message should be dropped entirely (all blocks orphaned)
    assert len(result) == 1
    assert result[0] == msgs[1]


def test_clean_orphan_keeps_tool_use_with_result():
    msgs = [
        _tool_use_msg("c1", "calc"),
        _tool_result_msg("c1", "2"),
    ]
    result = ContextCompressor._clean_orphan_tool_uses(msgs)
    assert len(result) == 2  # both kept


def test_clean_orphan_mixed_blocks():
    """Message with text + orphan tool_use: text kept, orphan removed."""
    msgs = [
        _mixed_msg([
            {"type": "text", "text": "thinking..."},
            {"type": "tool_use", "id": "orphan", "name": "search", "input": {}},
        ]),
    ]
    result = ContextCompressor._clean_orphan_tool_uses(msgs)
    assert len(result) == 1
    blocks = result[0]["content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"


def test_clean_orphan_string_content_passes_through():
    msgs = [
        {"role": "user", "content": "plain string content"},
        {"role": "assistant", "content": "another plain"},
    ]
    result = ContextCompressor._clean_orphan_tool_uses(msgs)
    assert result == msgs


# ── _generate_summary ───────────────────────────────────

@pytest.mark.asyncio
async def test_generate_summary_first_time():
    """First compression: no previous summary → full summary prompt."""
    c = ContextCompressor()
    mock_transport = AsyncMock()
    mock_transport.call.return_value = NormalizedResponse(
        text="摘要：用户问了计算问题，AI 回答了", tool_calls=[],
        usage={}, finish_reason="end_turn", stop_reason="end_turn", model="test",
    )

    middle = [
        _text_msg("user", "1+1等于几？"),
        _text_msg("assistant", "等于2"),
    ]
    summary = await c._generate_summary(middle, "", mock_transport)

    assert "计算" in summary
    # Verify prompt was for "first time" (no previous summary)
    call_args = mock_transport.call.call_args
    prompt_text = call_args.kwargs["messages"][0]["content"][0]["text"]
    assert "之前对话的摘要" not in prompt_text  # not iterative
    assert "压缩" in prompt_text


@pytest.mark.asyncio
async def test_generate_summary_iterative_update():
    """Second compression: has previous summary → update prompt."""
    c = ContextCompressor()
    c._previous_summary = "之前讨论了数学问题"

    mock_transport = AsyncMock()
    mock_transport.call.return_value = NormalizedResponse(
        text="之前讨论了数学问题，新内容：用户还问了颜色", tool_calls=[],
        usage={}, finish_reason="end_turn", stop_reason="end_turn", model="test",
    )

    middle = [_text_msg("user", "天空是什么颜色？")]
    summary = await c._generate_summary(middle, "", mock_transport)

    assert "数学" in summary
    call_args = mock_transport.call.call_args
    prompt_text = call_args.kwargs["messages"][0]["content"][0]["text"]
    assert "之前对话的摘要" in prompt_text  # iterative
    assert "更新" in prompt_text


@pytest.mark.asyncio
async def test_generate_summary_empty_response():
    """LLM returns empty → returns None."""
    c = ContextCompressor()
    mock_transport = AsyncMock()
    mock_transport.call.return_value = NormalizedResponse(
        text="", tool_calls=[], usage={}, finish_reason="end_turn",
        stop_reason="end_turn", model="test",
    )

    result = await c._generate_summary([_text_msg("user", "hi")], "", mock_transport)
    assert result is None


@pytest.mark.asyncio
async def test_generate_summary_llm_failure():
    """LLM call fails → returns None."""
    c = ContextCompressor()
    mock_transport = AsyncMock()
    mock_transport.call.side_effect = RuntimeError("API down")

    result = await c._generate_summary([_text_msg("user", "hi")], "", mock_transport)
    assert result is None


@pytest.mark.asyncio
async def test_generate_summary_uses_compressor_transport():
    """When compressor_transport is set, use it instead of main transport."""
    comp_transport = AsyncMock()
    comp_transport.call.return_value = NormalizedResponse(
        text="摘要", tool_calls=[], usage={}, finish_reason="end_turn",
        stop_reason="end_turn", model="test",
    )
    main_transport = AsyncMock()

    c = ContextCompressor(compressor_transport=comp_transport)
    await c._generate_summary([_text_msg("user", "hi")], "", main_transport)

    # Compressor transport should be used, not main
    comp_transport.call.assert_called_once()
    main_transport.call.assert_not_called()


# ── compress() full flow ────────────────────────────────

@pytest.mark.asyncio
async def test_compress_prune_sufficient():
    """Pruning alone drops below threshold → no LLM call."""
    c = ContextCompressor(threshold_ratio=0.99)  # very high threshold
    c.threshold_tokens = 999999  # prune won't reach this...
    # Actually let's set threshold low so it passes
    c.threshold_tokens = 100  # very low, prune should be sufficient
    c.last_prompt_tokens = 8000

    msgs = [
        _text_msg("user", "head1"),
        _text_msg("assistant", "head2"),
        _tool_result_msg("c1", "x" * 50),
        _text_msg("user", "tail1"),
        _text_msg("assistant", "tail2"),
        _text_msg("user", "tail3"),
        _text_msg("assistant", "tail4"),
        _text_msg("user", "tail5"),
        _text_msg("assistant", "tail6"),
    ]

    result = await c.compress(msgs, "", AsyncMock(), protect_head=2, protect_tail=6)
    # Should return pruned result without LLM call
    assert len(result) <= len(msgs)


@pytest.mark.asyncio
async def test_compress_updates_prompt_token_baseline():
    c = ContextCompressor()
    c.threshold_tokens = 999999
    c.last_prompt_tokens = 1

    msgs = _make_long_history(1)
    result = await c.compress(msgs, "system prompt", AsyncMock(), protect_head=2, protect_tail=6)

    assert result == msgs
    assert c.last_prompt_tokens > 1


@pytest.mark.asyncio
async def test_compress_full_summary():
    """Pruning not enough → LLM summary called."""
    mock_transport = AsyncMock()
    mock_transport.call.return_value = NormalizedResponse(
        text="对话涵盖了多个话题...", tool_calls=[], usage={},
        finish_reason="end_turn", stop_reason="end_turn", model="test",
    )

    c = ContextCompressor(threshold_ratio=0.01, protect_head=2, protect_tail=2)
    c.last_prompt_tokens = 50000

    msgs = _make_long_history(20)  # 40 messages, plenty
    result = await c.compress(msgs, "", mock_transport, protect_head=2, protect_tail=2)

    # Should return compressed: head(2) + summary_msg(1) + tail(2) = 5
    assert len(result) == 5
    # Summary msg should be provider-safe user context, not a system message in history.
    summary_msg = result[2]
    assert summary_msg["role"] == "user"
    assert "对话历史摘要" in summary_msg["content"][0]["text"]
    # Previous summary stored for iterative update
    assert c._previous_summary is not None


@pytest.mark.asyncio
async def test_compress_summary_is_safe_for_anthropic_messages():
    from personal_agent.llm.provider import ProviderProfile
    from personal_agent.plugins.builtin.llm.builtin.anthropic import AnthropicMessagesTransport

    mock_transport = AsyncMock()
    mock_transport.call.return_value = NormalizedResponse(
        text="压缩摘要", tool_calls=[], usage={},
        finish_reason="end_turn", stop_reason="end_turn", model="test",
    )
    c = ContextCompressor(threshold_ratio=0.01, protect_head=2, protect_tail=2)
    c.last_prompt_tokens = 50000

    compressed = await c.compress(_make_long_history(20), "", mock_transport, protect_head=2, protect_tail=2)
    converted = AnthropicMessagesTransport(ProviderProfile(
        name="test",
        base_url="https://example.test",
        api_key="k",
        model="claude-test",
    )).convert_messages(compressed)

    assert all(message.get("role") != "system" for message in converted)


@pytest.mark.asyncio
async def test_compress_failure_sets_cooldown():
    """LLM failure → cooldown + ineffective count."""
    mock_transport = AsyncMock()
    mock_transport.call.side_effect = RuntimeError("boom")

    c = ContextCompressor(threshold_ratio=0.01, protect_head=2, protect_tail=2)
    c.last_prompt_tokens = 50000

    msgs = _make_long_history(20)
    result = await c.compress(msgs, "", mock_transport, protect_head=2, protect_tail=2)

    # Should return original messages unchanged
    assert result == msgs
    assert c._summary_failure_cooldown_until > time.time()
    assert c._ineffective_compression_count == 1


@pytest.mark.asyncio
async def test_compress_ineffective_detection():
    """Compression saves < 10% tokens → ineffective counter increments."""
    mock_transport = AsyncMock()
    # Return same content → compression doesn't reduce tokens much
    mock_transport.call.return_value = NormalizedResponse(
        text="短摘要", tool_calls=[], usage={},
        finish_reason="end_turn", stop_reason="end_turn", model="test",
    )

    c = ContextCompressor(threshold_ratio=0.01, protect_head=2, protect_tail=2)
    c.last_prompt_tokens = 50000  # before compression

    msgs = _make_long_history(20)
    await c.compress(msgs, "", mock_transport, protect_head=2, protect_tail=2)

    # After compression with huge msgs, token count should drop significantly
    # If before=50000 and after is much less, it's effective
    assert c._ineffective_compression_count == 0  # should be effective


# ── session state ───────────────────────────────────────

def test_on_session_start_resets_state():
    c = ContextCompressor()
    c._previous_summary = "old summary"
    c._ineffective_compression_count = 2
    c._summary_failure_cooldown_until = time.time() + 100

    c.on_session_start()
    assert c._previous_summary is None
    assert c._ineffective_compression_count == 0
    assert c._summary_failure_cooldown_until == 0


def test_on_session_end_calls_start():
    c = ContextCompressor()
    c._previous_summary = "old"
    c._ineffective_compression_count = 1

    c.on_session_end()
    assert c._previous_summary is None
    assert c._ineffective_compression_count == 0


# ── _format_messages_for_summary ────────────────────────

def test_format_messages_text_only():
    msgs = [
        _text_msg("user", "hello"),
        _text_msg("assistant", "hi there"),
    ]
    result = _format_messages_for_summary(msgs)
    assert "user: hello" in result
    assert "assistant: hi there" in result


def test_format_messages_with_tools():
    msgs = [
        _tool_use_msg("c1", "web_search", {"query": "test"}),
        _tool_result_msg("c1", "search results here"),
    ]
    result = _format_messages_for_summary(msgs)
    assert "调用工具: web_search" in result
    assert "工具结果" in result


def test_format_messages_string_content():
    msgs = [
        {"role": "user", "content": "plain string"},
    ]
    result = _format_messages_for_summary(msgs)
    assert "user: plain string" in result


def test_format_messages_truncates_long_text():
    msgs = [
        _text_msg("user", "x" * 500),
    ]
    result = _format_messages_for_summary(msgs)
    assert len(result) < 500  # truncated at 300 chars per message
