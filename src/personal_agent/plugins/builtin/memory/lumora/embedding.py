"""OpenAI-compatible embedding client configured for Alibaba Bailian."""

from __future__ import annotations

import httpx

from personal_agent.text_safety import clean_text


class BailianEmbeddingClient:
    def __init__(self, config, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30)
        self.dimensions = int(config.dimensions or 0)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        values = [clean_text(text) for text in texts]
        payload = {"model": self.config.model, "input": values}
        if self.config.dimensions > 0:
            payload["dimensions"] = self.config.dimensions
        response = await self._client.post(
            self.config.base_url.rstrip("/") + "/embeddings",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            json=payload,
        )
        response.raise_for_status()
        data = response.json().get("data", [])
        ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
        vectors = [[float(value) for value in item["embedding"]] for item in ordered]
        if len(vectors) != len(values):
            raise RuntimeError("Embedding response count does not match input count")
        dimensions = len(vectors[0]) if vectors else 0
        if any(len(vector) != dimensions for vector in vectors):
            raise RuntimeError("Embedding response contains inconsistent dimensions")
        if self.dimensions and dimensions != self.dimensions:
            raise RuntimeError(f"Embedding dimension changed: expected {self.dimensions}, got {dimensions}")
        self.dimensions = dimensions
        return vectors

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
