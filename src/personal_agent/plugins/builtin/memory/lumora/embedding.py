"""OpenAI-compatible embedding backend."""

from __future__ import annotations

import httpx
from dataclasses import dataclass

from personal_agent.text_safety import clean_text
from personal_agent.plugins.builtin.memory.lumora.backends.base import BackendHealth


@dataclass(frozen=True)
class OpenAICompatibleEmbeddingConfig:
    base_url: str
    api_key: str
    model: str
    dimensions: int = 0
    timeout_seconds: float = 30.0


class OpenAICompatibleEmbeddingBackend:
    name = "openai_compatible"

    def __init__(self, config: OpenAICompatibleEmbeddingConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=config.timeout_seconds)
        self.dimensions = int(config.dimensions or 0)
        self.last_error = ""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        values = [clean_text(text) for text in texts]
        payload = {"model": self.config.model, "input": values}
        if self.config.dimensions > 0:
            payload["dimensions"] = self.config.dimensions
        try:
            response = await self._client.post(
                self.config.base_url.rstrip("/") + "/embeddings",
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                json=payload,
            )
            response.raise_for_status()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise
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
        self.last_error = ""
        return vectors

    def fingerprint(self) -> str:
        return f"{self.name}:{self.config.model}:{self.dimensions or self.config.dimensions or 'auto'}"

    def health_snapshot(self) -> BackendHealth:
        return BackendHealth(self.name, "failed" if self.last_error else "ready", self.last_error)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
