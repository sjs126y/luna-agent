"""Contract implemented by external long-term memory providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from personal_agent.memory.models import MemoryChange, MemoryRecord, MemoryReviewResult, MemoryScope, Observation


class ExternalMemoryProvider(ABC):
    name: str

    @abstractmethod
    async def review(self, messages: list[dict[str, Any]], scope: MemoryScope) -> MemoryReviewResult: ...

    @abstractmethod
    async def search(self, query: str, scope: MemoryScope, *, limit: int = 5) -> list[MemoryRecord]: ...

    @abstractmethod
    async def list(self, scope: MemoryScope, *, limit: int = 100) -> list[MemoryRecord]: ...

    @abstractmethod
    async def delete(self, memory_id: str, scope: MemoryScope) -> bool: ...

    @abstractmethod
    async def history(self, memory_id: str) -> list[MemoryChange]: ...

    @abstractmethod
    async def migrate(self, observations: tuple[Observation, ...], scope: MemoryScope) -> MemoryReviewResult: ...

    @abstractmethod
    def health_snapshot(self) -> dict[str, Any]: ...

    async def probe(self, scope: MemoryScope) -> None:
        await self.search("memory provider health probe", scope, limit=1)

    async def reindex(self, records: list[MemoryRecord], scope: MemoryScope) -> dict[str, int]:
        return {"attempted": 0, "completed": 0, "failed": 0}

    async def close(self) -> None:
        return None
