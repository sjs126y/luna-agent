"""OpenAI Responses transport behavior."""

from __future__ import annotations

import pytest

from luna_agent.llm.provider import ProviderProfile
from luna_agent.plugins.builtin.llm.builtin.responses import CodexResponsesTransport, OpenAIResponsesTransport


def _provider(reasoning_effort: str = "") -> ProviderProfile:
    return ProviderProfile(
        name="openai",
        base_url="https://api.example.test/v1",
        api_key="key",
        model="gpt-test",
        reasoning_effort=reasoning_effort,
    )


def test_responses_transport_converts_image_url_to_input_image():
    transport = OpenAIResponsesTransport(_provider())

    body = transport.build_request(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            }
        ],
        "system",
        [],
        512,
    )

    assert body["model"] == "gpt-test"
    assert body["max_output_tokens"] == 512
    assert body["input"][0] == {
        "role": "system",
        "content": [{"type": "input_text", "text": "system"}],
    }
    assert body["input"][1]["content"] == [
        {"type": "input_text", "text": "describe"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
    ]


def test_responses_transport_includes_reasoning_effort_when_configured():
    transport = OpenAIResponsesTransport(_provider(reasoning_effort="high"))

    body = transport.build_request(
        [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "system",
        [],
        512,
    )

    assert body["reasoning"] == {"effort": "high"}


def test_responses_transport_preserves_tool_call_chain():
    transport = OpenAIResponsesTransport(_provider())

    body = transport.build_request(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "我会读取文件。"},
                    {
                        "type": "tool_use",
                        "id": "call_read_1",
                        "name": "read",
                        "input": {"path": "AGENTS.md"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_read_1",
                        "content": "# AGENTS.md\nRepository Guidelines",
                    }
                ],
            },
        ],
        "",
        [],
        512,
    )

    assert body["input"] == [
        {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "我会读取文件。"}],
        },
        {
            "type": "function_call",
            "call_id": "call_read_1",
            "name": "read",
            "arguments": "{\"path\": \"AGENTS.md\"}",
        },
        {
            "type": "function_call_output",
            "call_id": "call_read_1",
            "output": "# AGENTS.md\nRepository Guidelines",
        },
    ]


def test_codex_responses_transport_textualizes_tool_call_chain_for_middle_stations():
    transport = CodexResponsesTransport(_provider())

    body = transport.build_request(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "我会读取文件。"},
                    {
                        "type": "tool_use",
                        "id": "call_read_1",
                        "name": "read",
                        "input": {"path": "AGENTS.md"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_read_1",
                        "content": "# AGENTS.md\nRepository Guidelines",
                    }
                ],
            },
        ],
        "",
        [],
        512,
    )

    assert body["input"] == [
        {
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": "我会读取文件。"},
                {
                    "type": "output_text",
                    "text": '[Tool call requested: read call_id=call_read_1 arguments={"path": "AGENTS.md"}]',
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "[Tool result for read call_id=call_read_1]\n# AGENTS.md\nRepository Guidelines",
                }
            ],
        },
    ]


def test_codex_responses_transport_alias_is_registered():
    from luna_agent.llm.transport_registry import transport_registry
    from luna_agent.plugins.builtin.llm.builtin import register

    register(None)

    assert isinstance(transport_registry.get("codex_responses", _provider()), CodexResponsesTransport)


@pytest.mark.asyncio
async def test_codex_responses_hides_flattened_analysis_channel():
    transport = CodexResponsesTransport(_provider())
    deltas: list[tuple[str, str]] = []

    async def events():
        yield {
            "model": "gpt-test",
            "output_text": "We need answer the user. assistant_final最终答复。",
            "status": "completed",
        }

    async def on_delta(kind: str, chunk: str) -> None:
        deltas.append((kind, chunk))

    response = await transport.parse_stream(events(), on_delta=on_delta)

    assert response.text == "最终答复。"
    assert deltas == [("text", "最终答复。")]


@pytest.mark.asyncio
async def test_codex_responses_returns_empty_when_final_channel_has_no_text():
    transport = CodexResponsesTransport(_provider())

    async def events():
        yield {
            "output_text": "We need answer in Chinese. assistant_final",
            "status": "completed",
        }

    response = await transport.parse_stream(events())

    assert response.text == ""


@pytest.mark.asyncio
async def test_codex_responses_always_uses_sse_for_middle_station(monkeypatch):
    import luna_agent.plugins.builtin.llm.builtin.responses as responses_module

    calls: list[bool] = []

    async def fake_call_openai_responses(**kwargs):
        calls.append(kwargs["stream"])
        yield {
            "type": "response.output_text.delta",
            "delta": "测试成功",
        }
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        }

    monkeypatch.setattr(
        responses_module,
        "call_openai_responses",
        fake_call_openai_responses,
    )
    transport = CodexResponsesTransport(_provider())

    response = await transport.call(
        [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        stream=False,
    )

    assert calls == [True]
    assert response.text == "测试成功"


@pytest.mark.asyncio
async def test_codex_responses_retries_stream_rate_limit_before_output(monkeypatch):
    import luna_agent.plugins.builtin.llm.builtin.responses as responses_module

    calls = 0
    delays: list[float] = []

    async def fake_call_openai_responses(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                "type": "response.failed",
                "response": {
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": "Concurrency limit exceeded for account, please retry later",
                    }
                },
            }
            return
        yield {"type": "response.output_text.delta", "delta": "重试成功"}
        yield {"type": "response.completed", "response": {"status": "completed"}}

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(responses_module, "call_openai_responses", fake_call_openai_responses)
    monkeypatch.setattr(responses_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(responses_module, "_response_retry_delay", lambda attempt: 1.0)

    response = await CodexResponsesTransport(_provider()).call(
        [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    )

    assert calls == 2
    assert delays == [1.0]
    assert response.text == "重试成功"


@pytest.mark.asyncio
async def test_responses_stream_rate_limit_stops_after_retry_budget(monkeypatch):
    import luna_agent.plugins.builtin.llm.builtin.responses as responses_module

    calls = 0

    async def fake_call_openai_responses(**kwargs):
        nonlocal calls
        calls += 1
        yield {
            "type": "response.failed",
            "error": {"code": "rate_limit_exceeded", "message": "try later"},
        }

    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(responses_module, "call_openai_responses", fake_call_openai_responses)
    monkeypatch.setattr(responses_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="rate_limit_exceeded"):
        await OpenAIResponsesTransport(_provider()).call(
            [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        )

    assert calls == 4


@pytest.mark.asyncio
async def test_responses_does_not_retry_failure_after_output(monkeypatch):
    import luna_agent.plugins.builtin.llm.builtin.responses as responses_module

    calls = 0

    async def fake_call_openai_responses(**kwargs):
        nonlocal calls
        calls += 1
        yield {"type": "response.output_text.delta", "delta": "partial"}
        yield {
            "type": "response.failed",
            "error": {"code": "rate_limit_exceeded", "message": "try later"},
        }

    monkeypatch.setattr(responses_module, "call_openai_responses", fake_call_openai_responses)

    with pytest.raises(RuntimeError, match="rate_limit_exceeded"):
        await OpenAIResponsesTransport(_provider()).call(
            [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        )

    assert calls == 1


@pytest.mark.asyncio
async def test_responses_transport_parses_non_stream_output_text():
    transport = OpenAIResponsesTransport(_provider())

    async def events():
        yield {
            "model": "gpt-test",
            "output_text": "done",
            "status": "completed",
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }

    response = await transport.parse_stream(events())

    assert response.text == "done"
    assert response.model == "gpt-test"
    assert response.finish_reason == "completed"
    assert response.usage["input_tokens"] == 3
    assert response.usage["output_tokens"] == 2


@pytest.mark.asyncio
async def test_responses_transport_parses_output_content_blocks():
    transport = OpenAIResponsesTransport(_provider())

    async def events():
        yield {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "hello"},
                        {"type": "output_text", "text": " world"},
                    ],
                }
            ],
        }

    response = await transport.parse_stream(events())

    assert response.text == "hello world"


@pytest.mark.asyncio
async def test_responses_transport_parses_non_stream_function_call():
    transport = OpenAIResponsesTransport(_provider())

    async def events():
        yield {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "web_search",
                    "arguments": "{\"query\":\"gpt5.5\",\"max_results\":3}",
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 4},
        }

    response = await transport.parse_stream(events())

    assert response.text == ""
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls == [
        {
            "id": "call_1",
            "name": "web_search",
            "input": {"query": "gpt5.5", "max_results": 3},
        }
    ]


@pytest.mark.asyncio
async def test_responses_transport_parses_completed_event_function_call():
    transport = OpenAIResponsesTransport(_provider())

    async def events():
        yield {
            "type": "response.completed",
            "response": {
                "model": "gpt-test",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_2",
                        "name": "read",
                        "arguments": {"path": "CLAUDE.md"},
                    }
                ],
            },
        }

    response = await transport.parse_stream(events())

    assert response.model == "gpt-test"
    assert response.tool_calls == [
        {
            "id": "call_2",
            "name": "read",
            "input": {"path": "CLAUDE.md"},
        }
    ]


@pytest.mark.asyncio
async def test_responses_call_streams_when_delta_callback_is_present(monkeypatch):
    transport = OpenAIResponsesTransport(_provider())
    seen: dict[str, object] = {}
    deltas: list[tuple[str, str]] = []

    async def fake_call_openai_responses(**kwargs):
        seen.update(kwargs)
        yield {"type": "response.output_text.delta", "delta": "Hel"}
        yield {"type": "response.output_text.delta", "delta": "lo"}
        yield {
            "type": "response.completed",
            "response": {"model": "gpt-test", "status": "completed"},
        }

    async def on_delta(kind: str, chunk: str) -> None:
        deltas.append((kind, chunk))

    monkeypatch.setattr(
        "luna_agent.plugins.builtin.llm.builtin.responses.call_openai_responses",
        fake_call_openai_responses,
    )

    response = await transport.call(
        messages=[{"role": "user", "content": "hi"}],
        system_prompt="system",
        tools=[],
        on_delta=on_delta,
    )

    assert seen["stream"] is True
    assert response.text == "Hello"
    assert deltas == [("text", "Hel"), ("text", "lo")]
