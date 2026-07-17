from __future__ import annotations

import httpx
import pytest

from personal_agent.plugins.builtin.memory.lumora.embedding import (
    OpenAICompatibleEmbeddingBackend,
    OpenAICompatibleEmbeddingConfig,
)


@pytest.mark.asyncio
async def test_bailian_embedding_uses_openai_compatible_endpoint() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["authorization"]
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = OpenAICompatibleEmbeddingConfig(
        base_url="https://dashscope.example/v1",
        api_key="secret",
        model="text-embedding-v4",
    )
    client = OpenAICompatibleEmbeddingBackend(config, client=http)

    vectors = await client.embed(["first", "second"])

    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert seen["url"] == "https://dashscope.example/v1/embeddings"
    assert seen["authorization"] == "Bearer secret"
    assert client.dimensions == 2
    await http.aclose()
