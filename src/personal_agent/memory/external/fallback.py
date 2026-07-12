"""Dependency-free external memory fallback using SQLite FTS5."""

from __future__ import annotations

from typing import Any

from personal_agent.memory.external.base import ExternalMemoryProvider
from personal_agent.memory.models import (
    MemoryChange,
    MemoryChangeAction,
    MemoryRecord,
    MemoryReviewResult,
    MemoryScope,
)


class FallbackMemoryProvider(ExternalMemoryProvider):
    name = "fallback"

    def __init__(self, archive, llm) -> None:
        self.archive = archive
        self.llm = llm
        self.last_error = ""

    async def review(self, messages: list[dict[str, Any]], scope: MemoryScope) -> MemoryReviewResult:
        batch_id = await self.archive.create_review_batch(
            scope, requested=self.name, effective=self.name
        )
        try:
            observations = await self.llm.extract_observations(messages)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            await self.archive.finish_review_batch(batch_id, status="pending_extraction", error=self.last_error)
            return MemoryReviewResult(provider=self.name, batch_id=batch_id)
        await self.archive.save_observations(
            scope, observations, batch_id=batch_id, migration_status="pending"
        )
        changes: list[MemoryChange] = []
        for observation in observations:
            if await self.archive.find_memory_by_content(scope, observation.content):
                changes.append(MemoryChange(
                    action=MemoryChangeAction.NONE, observation_id=observation.id,
                    reason="exact duplicate",
                ))
                continue
            record = MemoryRecord(
                id=observation.id, content=observation.content, kind=observation.kind,
                importance=observation.importance, provider=self.name, scope=scope,
                created_at=observation.created_at, updated_at=observation.created_at,
            )
            await self.archive.upsert_memory(scope, record)
            changes.append(MemoryChange(
                action=MemoryChangeAction.ADD, observation_id=observation.id,
                memory_id=record.id, content=record.content,
            ))
        self.last_error = ""
        await self.archive.finish_review_batch(batch_id, status="completed")
        return MemoryReviewResult(
            observations=observations, changes=tuple(changes), provider=self.name, batch_id=batch_id
        )

    async def search(self, query: str, scope: MemoryScope, *, limit: int = 5) -> list[MemoryRecord]:
        return await self.archive.search_bm25(scope, query, limit=limit)

    async def list(self, scope: MemoryScope, *, limit: int = 100) -> list[MemoryRecord]:
        return await self.archive.list_memories(scope, limit=limit)

    async def delete(self, memory_id: str, scope: MemoryScope) -> bool:
        return await self.archive.delete_memory(memory_id, scope, provider=self.name)

    async def history(self, memory_id: str) -> list[MemoryChange]:
        return [_history_change(item) for item in await self.archive.memory_history(memory_id)]

    async def migrate(self, observations, scope: MemoryScope) -> MemoryReviewResult:
        await self.archive.save_observations(scope, tuple(observations), migration_status="pending")
        return MemoryReviewResult(observations=tuple(observations), provider=self.name)

    def health_snapshot(self) -> dict[str, Any]:
        return {"provider": self.name, "available": True, "last_error": self.last_error}


def _history_change(item: dict[str, Any]) -> MemoryChange:
    return MemoryChange(
        action=MemoryChangeAction(item["action"]), observation_id="",
        memory_id=item["memory_id"], content=item["content"],
        previous_content=item["previous_content"], reason=item["reason"],
        created_at=item["created_at"],
    )
