"""Text safety guards at JSON/API boundaries."""

from __future__ import annotations

import pytest

from luna_agent.text_safety import clean_payload, clean_text


def test_clean_text_replaces_unpaired_surrogates():
    assert clean_text("bad \ud83d text") == "bad ? text"


def test_clean_payload_recursively_replaces_bad_strings():
    payload = {"messages": [{"content": [{"type": "text", "text": "bad \ud83d"}]}]}

    cleaned = clean_payload(payload)

    assert cleaned["messages"][0]["content"][0]["text"] == "bad ?"


@pytest.mark.asyncio
async def test_llm_call_sanitizes_body_before_httpx_json_encoding(monkeypatch):
    from luna_agent.llm import client as llm_client

    captured = {}

    class Response:
        status_code = 200
        headers = {}

        async def aread(self):
            return b""

        async def aiter_lines(self):
            yield 'data: {"type":"message_stop"}'

    class StreamContext:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, *args):
            return None

    class AsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, method, url, *, json, headers):
            captured["json"] = json
            return StreamContext()

    monkeypatch.setattr(llm_client.httpx, "AsyncClient", AsyncClient)

    events = [
        event async for event in llm_client.call_anthropic(
            "https://example.test",
            "key",
            {"messages": [{"role": "user", "content": "bad \ud83d"}]},
        )
    ]

    assert events == [{"type": "message_stop"}]
    assert captured["json"]["messages"][0]["content"] == "bad ?"
