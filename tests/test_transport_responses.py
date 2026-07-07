"""OpenAI Responses transport behavior."""

from __future__ import annotations

import pytest

from personal_agent.llm.provider import ProviderProfile
from personal_agent.plugins.builtin.llm.builtin.responses import CodexResponsesTransport, OpenAIResponsesTransport


def _provider() -> ProviderProfile:
    return ProviderProfile(
        name="openai",
        base_url="https://api.example.test/v1",
        api_key="key",
        model="gpt-test",
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


def test_codex_responses_transport_alias_is_registered():
    from personal_agent.llm.transport_registry import transport_registry
    from personal_agent.plugins.builtin.llm.builtin import register

    register(None)

    assert isinstance(transport_registry.get("codex_responses", _provider()), CodexResponsesTransport)


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
