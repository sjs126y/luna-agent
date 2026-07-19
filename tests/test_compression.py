"""Codex-style context compaction tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from luna_agent.agent.context import _check_and_compress
from luna_agent.compression import CompactionResult, ContextEngine, compression_registry
from luna_agent.compression.simple import (
    HANDOFF_SYSTEM_PROMPT,
    LEGACY_SUMMARY_PREFIX,
    SUMMARY_PREFIX,
    ContextCompressor,
    _format_messages_for_summary,
    _fit_handoff_input,
    is_summary_message,
)
from luna_agent.models.messages import NormalizedResponse


def _text(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _tool_call(call_id: str, name: str = "search") -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": call_id, "name": name, "input": {"q": "x"}}],
    }


def _tool_result(call_id: str, value: str = "result") -> dict:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": call_id, "content": value}],
    }


def _response(text: str, *, input_tokens: int = 0, output_tokens: int = 0):
    return NormalizedResponse(
        text=text,
        tool_calls=[],
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
        finish_reason="end_turn",
        stop_reason="end_turn",
        model="test",
    )


def test_context_engine_contract_and_registry():
    assert hasattr(ContextEngine, "compress")
    assert compression_registry.list_engines() == ["simple"]
    assert compression_registry.get("simple") is compression_registry.get("compressor")


def test_compressor_defaults_have_no_summary_specific_cap():
    compressor = ContextCompressor()
    assert compressor.threshold_tokens == int(256_000 * 0.9)
    assert compressor.tail_token_budget == 20_000
    assert compressor.retained_user_tokens == 20_000
    assert not hasattr(compressor, "max_summary_tokens")
    assert not hasattr(compressor, "_previous_summary")


def test_should_compress_only_uses_context_threshold():
    compressor = ContextCompressor(context_length=20_000, threshold_ratio=0.5, output_tokens=100)
    messages = [_text("user", "one"), _text("assistant", "two")]
    assert compressor.should_compress(9_999, messages) is False
    assert compressor.should_compress(10_000, messages) is True
    assert compressor.should_compress(19_000, messages[:1]) is False


def test_update_from_response_tracks_provider_usage():
    compressor = ContextCompressor()
    compressor.update_from_response(_response("ok", input_tokens=4321))
    assert compressor.last_prompt_tokens == 4321


def test_summary_markers_recognize_current_and_legacy_messages():
    assert is_summary_message(_text("user", SUMMARY_PREFIX + "new"))
    assert is_summary_message(_text("user", LEGACY_SUMMARY_PREFIX + "old"))
    assert not is_summary_message(_text("user", "real user text"))


def test_formatter_preserves_long_text_and_complete_tool_results():
    long_text = "x" * 2000
    formatted = _format_messages_for_summary([
        _text("user", long_text),
        _tool_call("call-1", "web_search"),
        _tool_result("call-1", "y" * 1000),
    ])
    assert long_text in formatted
    assert "web_search" in formatted
    assert "y" * 1000 in formatted


def test_overflow_recovery_reduces_old_tool_blob_but_preserves_user_text():
    user_text = "critical architecture decision"
    messages = [
        _text("user", user_text),
        _tool_call("large"),
        _tool_result("large", "BEGIN-" + "x" * 20_000 + "-END"),
        _text("assistant", "continue"),
    ]

    fitted, reduced = _fit_handoff_input(messages, token_budget=1000, model="test")

    serialized = str(fitted)
    assert reduced == 1
    assert user_text in serialized
    assert "omitted during context recovery" in serialized
    assert "BEGIN-" in serialized
    assert "-END" in serialized
    assert "omitted during context recovery" not in str(messages)


@pytest.mark.asyncio
async def test_handoff_prompt_has_no_length_instruction_or_compaction_cap():
    transport = AsyncMock()
    transport.call.return_value = _response("complete handoff")
    compressor = ContextCompressor(output_tokens=16_384)

    summary = await compressor._generate_summary([_text("user", "detail")], transport)

    assert summary == "complete handoff"
    kwargs = transport.call.call_args.kwargs
    assert kwargs["system_prompt"] == HANDOFF_SYSTEM_PROMPT
    assert "300" not in kwargs["system_prompt"]
    assert "token limit" in kwargs["system_prompt"]
    assert kwargs["max_tokens"] == 16_384


@pytest.mark.asyncio
async def test_configured_compressor_transport_is_used():
    dedicated = AsyncMock()
    dedicated.call.return_value = _response("handoff")
    main = AsyncMock()
    compressor = ContextCompressor(compressor_transport=dedicated)

    await compressor._generate_summary([_text("user", "hello")], main)

    dedicated.call.assert_awaited_once()
    main.call.assert_not_called()


@pytest.mark.asyncio
async def test_compress_builds_replacement_with_one_new_summary():
    transport = AsyncMock()
    transport.call.return_value = _response("new complete handoff")
    compressor = ContextCompressor(
        tail_token_budget=80,
        retained_user_tokens=5000,
        model="test",
    )
    messages = [
        _text("user", "original requirement"),
        _text("assistant", "old answer " + "a" * 500),
        _text("user", LEGACY_SUMMARY_PREFIX + "previous checkpoint"),
        _text("user", "latest correction"),
        _text("assistant", "latest answer"),
    ]

    result = await compressor.compress(messages, "system", transport)

    assert isinstance(result, CompactionResult)
    texts = [str(message) for message in result.replacement_history]
    assert any("original requirement" in text for text in texts)
    assert any("latest correction" in text for text in texts)
    assert not any("previous checkpoint" in text for text in texts)
    assert sum(is_summary_message(message) for message in result.replacement_history) == 1
    assert result.metadata.pre_message_count == len(messages)
    assert result.metadata.post_message_count == len(result.replacement_history)
    prompt = transport.call.call_args.kwargs["messages"][0]["content"][0]["text"]
    assert "previous checkpoint" in prompt


@pytest.mark.asyncio
async def test_recent_tool_call_and_result_are_retained_as_pair():
    transport = AsyncMock()
    transport.call.return_value = _response("handoff")
    compressor = ContextCompressor(tail_token_budget=10_000)
    messages = [
        _text("user", "run it"),
        _tool_call("call-1"),
        _tool_result("call-1", "done"),
        _text("assistant", "finished"),
    ]

    result = await compressor.compress(messages, "", transport)

    serialized = str(result.replacement_history)
    assert "call-1" in serialized
    assert "done" in serialized


@pytest.mark.asyncio
async def test_empty_summary_fails_without_truncating_history():
    transport = AsyncMock()
    transport.call.return_value = _response("")
    compressor = ContextCompressor()
    with pytest.raises(RuntimeError, match="empty handoff"):
        await compressor.compress([_text("user", "keep me"), _text("assistant", "ok")], "", transport)


@pytest.mark.asyncio
async def test_pre_compact_hook_can_defer_compression_and_add_context():
    from luna_agent.hooks import ContextHookOutcome, HookEvent, HookManager

    hooks = HookManager()

    async def defer(event):
        assert event.payload["trigger"] == "auto"
        return ContextHookOutcome(additional_context="preserve investigation", stop=True)

    hooks.register(owner="test", event=HookEvent.PRE_COMPACT, callback=defer)
    compressor = MagicMock()
    compressor.should_compress.return_value = True
    compressor.compress = AsyncMock()
    agent = SimpleNamespace(
        _compressor=compressor,
        _provider=SimpleNamespace(model="test"),
        _cached_system_prompt="system",
        tools=[],
        _hook_manager=hooks,
        _hook_turn_id="turn-hook",
        _hook_additional_contexts=[],
        _hook_source=None,
        _security_context=None,
        _memory_session_key="session-hook",
        _transport=AsyncMock(),
    )
    messages = [_text("user", "a"), _text("assistant", "b")]

    result = await _check_and_compress(agent, messages)

    assert result is messages
    compressor.compress.assert_not_awaited()
    assert agent._hook_additional_contexts == [
        "[PreCompact hook context]\npreserve investigation"
    ]


@pytest.mark.asyncio
async def test_legacy_engine_list_result_remains_supported():
    compressor = MagicMock()
    compressor.should_compress.return_value = True
    compressor.compress = AsyncMock(return_value=[_text("user", "legacy replacement")])
    agent = SimpleNamespace(
        _compressor=compressor,
        _provider=SimpleNamespace(model="test"),
        _cached_system_prompt="system",
        tools=[],
        _hook_manager=None,
        _hook_turn_id="",
        _hook_additional_contexts=[],
        _transport=AsyncMock(),
    )
    messages = [_text("user", "a"), _text("assistant", "b")]

    result = await _check_and_compress(agent, messages)

    assert result == [_text("user", "legacy replacement")]


@pytest.mark.asyncio
async def test_compression_failure_preserves_original_messages():
    compressor = MagicMock()
    compressor.should_compress.return_value = True
    compressor.compress = AsyncMock(side_effect=RuntimeError("down"))
    agent = SimpleNamespace(
        _compressor=compressor,
        _provider=SimpleNamespace(model="test"),
        _cached_system_prompt="system",
        tools=[],
        _hook_manager=None,
        _hook_turn_id="",
        _hook_additional_contexts=[],
        _transport=AsyncMock(),
    )
    messages = [_text("user", "keep"), _text("assistant", "all")]

    result = await _check_and_compress(agent, messages)

    assert result is messages
