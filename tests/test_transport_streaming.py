"""Streaming delta callback + thinking capture in transports (Phase 1)."""

from __future__ import annotations

import pytest

from personal_agent.llm.provider import ProviderProfile
from personal_agent.plugins.builtin.llm.builtin.anthropic import AnthropicMessagesTransport
from personal_agent.plugins.builtin.llm.builtin.chat_completions import (
    ChatCompletionsTransport,
)


def _provider(model: str = "deepseek-v4-pro") -> ProviderProfile:
    return ProviderProfile(
        name="test",
        base_url="https://example.test/anthropic",
        api_key="k",
        model=model,
    )


async def _aiter(events):
    for event in events:
        yield event


# ── Anthropic transport ─────────────────────────────────


def _anthropic_stream():
    """A minimal Anthropic SSE stream: thinking block, then a text block."""
    return [
        {"type": "message_start", "message": {"model": "deepseek-v4-pro", "usage": {"input_tokens": 10}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "先想"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "一下"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "你好"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "世界"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]


@pytest.mark.asyncio
async def test_anthropic_parse_stream_fires_text_and_thinking_deltas():
    transport = AnthropicMessagesTransport(_provider())
    deltas: list[tuple[str, str]] = []

    async def on_delta(kind: str, chunk: str) -> None:
        deltas.append((kind, chunk))

    resp = await transport.parse_stream(_aiter(_anthropic_stream()), on_delta=on_delta)

    assert resp.text == "你好世界"
    assert resp.thinking == "先想一下"
    assert ("thinking", "先想") in deltas
    assert ("thinking", "一下") in deltas
    assert ("text", "你好") in deltas
    assert ("text", "世界") in deltas
    # Thinking arrives before text.
    assert deltas.index(("thinking", "先想")) < deltas.index(("text", "你好"))


@pytest.mark.asyncio
async def test_anthropic_parse_stream_without_callback_is_unchanged():
    transport = AnthropicMessagesTransport(_provider())

    resp = await transport.parse_stream(_aiter(_anthropic_stream()))

    assert resp.text == "你好世界"
    assert resp.thinking == "先想一下"
    assert resp.finish_reason == "end_turn"


@pytest.mark.asyncio
async def test_anthropic_parse_stream_handles_non_streaming_message():
    """DeepSeek non-streaming returns a single 'message' event with content blocks."""
    transport = AnthropicMessagesTransport(_provider())
    deltas: list[tuple[str, str]] = []

    events = [
        {
            "type": "message",
            "model": "deepseek-v4-pro",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 4},
            "content": [
                {"type": "thinking", "thinking": "想法"},
                {"type": "text", "text": "答案"},
            ],
        }
    ]

    async def on_delta(kind: str, chunk: str) -> None:
        deltas.append((kind, chunk))

    resp = await transport.parse_stream(_aiter(events), on_delta=on_delta)

    assert resp.text == "答案"
    assert resp.thinking == "想法"
    assert ("thinking", "想法") in deltas
    assert ("text", "答案") in deltas


@pytest.mark.asyncio
async def test_anthropic_parse_stream_tool_calls_still_work():
    transport = AnthropicMessagesTransport(_provider())
    events = [
        {"type": "message_start", "message": {"model": "m", "usage": {"input_tokens": 1}}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "t1", "name": "web_search"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"query":'},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '"hi"}'},
        },
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 2}},
        {"type": "message_stop"},
    ]

    resp = await transport.parse_stream(_aiter(events))

    assert resp.tool_calls == [{"id": "t1", "name": "web_search", "input": {"query": "hi"}}]
    assert resp.finish_reason == "tool_use"


# ── ChatCompletions transport ───────────────────────────


@pytest.mark.asyncio
async def test_chat_completions_parse_stream_fires_text_deltas():
    transport = ChatCompletionsTransport(_provider(model="gpt-4o"))
    deltas: list[tuple[str, str]] = []

    events = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"finish_reason": "stop", "delta": {}}]},
        {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 1}},
    ]

    async def on_delta(kind: str, chunk: str) -> None:
        deltas.append((kind, chunk))

    resp = await transport.parse_stream(_aiter(events), on_delta=on_delta)

    assert resp.text == "Hello"
    assert ("text", "Hel") in deltas
    assert ("text", "lo") in deltas


@pytest.mark.asyncio
async def test_chat_completions_parse_stream_without_callback_is_unchanged():
    transport = ChatCompletionsTransport(_provider(model="gpt-4o"))
    events = [
        {"choices": [{"delta": {"content": "Hello"}}]},
        {"choices": [{"finish_reason": "stop", "delta": {}}]},
    ]

    resp = await transport.parse_stream(_aiter(events))

    assert resp.text == "Hello"
    assert resp.thinking == ""
