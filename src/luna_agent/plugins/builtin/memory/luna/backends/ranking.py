"""Candidate fusion and optional reranking for Luna."""

from __future__ import annotations

from luna_agent.memory.models import MemoryRecord
from luna_agent.plugins.builtin.memory.luna.backends.base import (
    BackendHealth,
    RankedMemory,
    SearchHit,
)


class WeightedRrfFusion:
    name = "weighted_rrf"

    def __init__(
        self,
        *,
        semantic_weight: float = 0.6,
        keyword_weight: float = 0.4,
        importance_weight: float = 0.1,
        rrf_k: int = 60,
    ) -> None:
        self.weights = {"semantic": semantic_weight, "keyword": keyword_weight}
        self.importance_weight = importance_weight
        self.rrf_k = rrf_k

    async def fuse(
        self,
        query: str,
        records: dict[str, MemoryRecord],
        result_sets: dict[str, list[SearchHit]],
        *,
        limit: int,
    ) -> list[RankedMemory]:
        del query
        scores: dict[str, float] = {}
        sources: dict[str, set[str]] = {}
        raw_scores: dict[str, dict[str, float | None]] = {}
        for source, hits in result_sets.items():
            weight = self.weights.get(source, 1.0)
            for hit in hits:
                scores[hit.memory_id] = scores.get(hit.memory_id, 0.0) + weight / (self.rrf_k + hit.rank)
                sources.setdefault(hit.memory_id, set()).add(source)
                raw_scores.setdefault(hit.memory_id, {})[source] = hit.score
        ranked: list[RankedMemory] = []
        for memory_id, score in scores.items():
            record = records.get(memory_id)
            if record is None:
                continue
            importance_factor = 1.0 - self.importance_weight + self.importance_weight * record.importance
            ranked.append(RankedMemory(
                memory_id=memory_id,
                score=score * importance_factor,
                sources=tuple(sorted(sources.get(memory_id, set()))),
                metadata={"raw_scores": raw_scores.get(memory_id, {})},
            ))
        return sorted(ranked, key=lambda item: item.score, reverse=True)[:limit]

    def fingerprint(self) -> str:
        return (
            f"{self.name}:{self.weights['semantic']}:{self.weights['keyword']}:"
            f"{self.importance_weight}:{self.rrf_k}"
        )

    def health_snapshot(self) -> BackendHealth:
        return BackendHealth(self.name)

    async def close(self) -> None:
        return None


class NoOpReranker:
    name = "none"

    async def rerank(
        self,
        query: str,
        candidates: list[RankedMemory],
        records: dict[str, MemoryRecord],
        *,
        limit: int,
    ) -> list[RankedMemory]:
        del query, records
        return candidates[:limit]

    def fingerprint(self) -> str:
        return self.name

    def health_snapshot(self) -> BackendHealth:
        return BackendHealth(self.name, "disabled")

    async def close(self) -> None:
        return None
