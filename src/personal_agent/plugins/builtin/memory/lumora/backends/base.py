"""Small contracts shared by Lumora retrieval backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from personal_agent.memory.models import MemoryRecord, MemoryScope


@dataclass(frozen=True)
class BackendSelection:
    provider: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BackendHealth:
    provider: str
    status: str = "ready"
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class SearchHit:
    memory_id: str
    source: str
    rank: int
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RankedMemory:
    memory_id: str
    score: float
    sources: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


class EmbeddingBackend(Protocol):
    name: str
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    def fingerprint(self) -> str: ...

    def health_snapshot(self) -> BackendHealth: ...

    async def close(self) -> None: ...


class VectorIndexBackend(Protocol):
    name: str

    async def initialize(self, dimensions: int) -> None: ...

    async def search(
        self,
        vector: list[float],
        scope: MemoryScope,
        *,
        limit: int,
    ) -> list[SearchHit]: ...

    async def upsert(self, memory: MemoryRecord, vector: list[float]) -> None: ...

    async def delete(self, memory_id: str) -> None: ...

    def fingerprint(self) -> str: ...

    def health_snapshot(self) -> BackendHealth: ...

    async def close(self) -> None: ...


class KeywordIndexBackend(Protocol):
    name: str

    async def search(
        self,
        query: str,
        scope: MemoryScope,
        *,
        limit: int,
    ) -> list[SearchHit]: ...

    async def upsert(self, memory: MemoryRecord) -> None: ...

    async def delete(self, memory_id: str) -> None: ...

    def fingerprint(self) -> str: ...

    def health_snapshot(self) -> BackendHealth: ...

    async def close(self) -> None: ...


class FusionStrategy(Protocol):
    name: str

    async def fuse(
        self,
        query: str,
        records: dict[str, MemoryRecord],
        result_sets: dict[str, list[SearchHit]],
        *,
        limit: int,
    ) -> list[RankedMemory]: ...

    def fingerprint(self) -> str: ...

    def health_snapshot(self) -> BackendHealth: ...

    async def close(self) -> None: ...


class RerankerBackend(Protocol):
    name: str

    async def rerank(
        self,
        query: str,
        candidates: list[RankedMemory],
        records: dict[str, MemoryRecord],
        *,
        limit: int,
    ) -> list[RankedMemory]: ...

    def fingerprint(self) -> str: ...

    def health_snapshot(self) -> BackendHealth: ...

    async def close(self) -> None: ...
