"""Keyword indexes bundled with Luna."""

from __future__ import annotations

from luna_agent.memory.models import MemoryRecord, MemoryScope
from luna_agent.plugins.builtin.memory.luna.backends.base import BackendHealth, SearchHit


class SqliteFts5KeywordIndex:
    name = "sqlite_fts5"

    def __init__(self, archive, *, tokenizer: str = "unicode61") -> None:
        self.archive = archive
        self.tokenizer = tokenizer

    async def search(self, query: str, scope: MemoryScope, *, limit: int) -> list[SearchHit]:
        records = await self.archive.search_bm25(scope, query, limit=limit)
        return [
            SearchHit(item.id, "keyword", rank, item.score)
            for rank, item in enumerate(records, start=1)
        ]

    async def upsert(self, memory: MemoryRecord) -> None:
        # MemoryArchive updates its fallback FTS index in the same transaction as the record.
        return None

    async def delete(self, memory_id: str) -> None:
        return None

    def fingerprint(self) -> str:
        return f"{self.name}:{self.tokenizer}"

    def health_snapshot(self) -> BackendHealth:
        return BackendHealth(self.name)

    async def close(self) -> None:
        return None
