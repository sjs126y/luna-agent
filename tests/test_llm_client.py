from __future__ import annotations

import pytest

from personal_agent.llm import client as llm_client


@pytest.mark.asyncio
async def test_chat_completions_non_json_response_raises_stream_error(monkeypatch):
    class Response:
        status_code = 200
        text = "<!doctype html><html></html>"
        headers = {"content-type": "text/html; charset=utf-8"}

        def json(self):
            raise llm_client.json.JSONDecodeError("Expecting value", self.text, 0)

    class AsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(llm_client.httpx, "AsyncClient", AsyncClient)

    with pytest.raises(llm_client.StreamError) as exc_info:
        async for _ in llm_client.call_chat_completions(
            "https://api.example.test/v1",
            "key",
            {"model": "m", "messages": []},
            stream=False,
        ):
            pass

    assert "Non-JSON response from LLM API" in str(exc_info.value)
    assert "text/html" in str(exc_info.value)
