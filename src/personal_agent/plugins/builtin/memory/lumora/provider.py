"""Lumora long-term memory with LLM resolution and hybrid retrieval."""

from __future__ import annotations

from dataclasses import replace
import json
from typing import Any

from personal_agent.memory.external.base import ExternalMemoryProvider
from personal_agent.memory.llm import MemoryLLMFacade
from personal_agent.memory.models import (
    MemoryChange, MemoryChangeAction, MemoryRecord, MemoryReviewResult, MemoryScope,
)
from personal_agent.memory.prompts import MEMORY_RESOLUTION_SYSTEM


class LumoraMemoryProvider(ExternalMemoryProvider):
    name = "lumora"

    def __init__(self, *, archive, context, embedding, vector_index, llm=None) -> None:
        self.archive = archive
        self.context = context
        self.embedding = embedding
        self.vector_index = vector_index
        self.llm = llm or MemoryLLMFacade(context.llm)
        self.last_error = ""

    async def review(self, messages: list[dict[str, Any]], scope: MemoryScope) -> MemoryReviewResult:
        batch_id = await self.archive.create_review_batch(scope, requested=self.name, effective=self.name)
        observations = await self.llm.extract_observations(messages)
        await self.archive.save_observations(scope, observations, batch_id=batch_id)
        changes: list[MemoryChange] = []
        for observation in observations:
            related = await self.search(observation.content, scope, limit=5)
            change = await self._resolve(observation, related)
            await self._apply_change(scope, observation, change, related)
            changes.append(change)
        await self.archive.finish_review_batch(batch_id, status="completed")
        return MemoryReviewResult(observations, tuple(changes), self.name, batch_id)

    async def search(self, query: str, scope: MemoryScope, *, limit: int = 5) -> list[MemoryRecord]:
        vector = (await self.embedding.embed([query]))[0]
        semantic = await self.vector_index.search(
            vector, user_id=scope.user_id, profile=scope.profile, limit=max(limit * 3, 10)
        )
        keyword = await self.archive.search_bm25(scope, query, limit=max(limit * 3, 10))
        semantic_ids = [item[0] for item in semantic]
        keyword_ids = [item.id for item in keyword]
        scores = reciprocal_rank_fusion(semantic_ids, keyword_ids)
        records = await self.archive.get_memories(
            sorted(scores, key=scores.get, reverse=True), scope
        )
        ranked = [replace(item, score=scores[item.id] * (0.9 + 0.1 * item.importance)) for item in records]
        return sorted(ranked, key=lambda item: item.score, reverse=True)[:limit]

    async def list(self, scope: MemoryScope, *, limit: int = 100) -> list[MemoryRecord]:
        return await self.archive.list_memories(scope, limit=limit)

    async def delete(self, memory_id: str, scope: MemoryScope) -> bool:
        deleted = await self.archive.delete_memory(memory_id, scope, provider=self.name)
        if deleted:
            await self.vector_index.delete(memory_id)
        return deleted

    async def history(self, memory_id: str) -> list[MemoryChange]:
        result = []
        for item in await self.archive.memory_history(memory_id):
            result.append(MemoryChange(
                action=MemoryChangeAction(item["action"]), observation_id="", memory_id=memory_id,
                content=item["content"], previous_content=item["previous_content"],
                reason=item["reason"], created_at=item["created_at"],
            ))
        return result

    async def migrate(self, observations, scope: MemoryScope) -> MemoryReviewResult:
        changes: list[MemoryChange] = []
        for observation in observations:
            related = await self.search(observation.content, scope, limit=5)
            change = await self._resolve(observation, related)
            await self._apply_change(scope, observation, change, related)
            changes.append(change)
        return MemoryReviewResult(tuple(observations), tuple(changes), self.name)

    def health_snapshot(self) -> dict[str, Any]:
        return {"provider": self.name, "available": not self.last_error, "last_error": self.last_error}

    async def close(self) -> None:
        await self.embedding.close()
        await self.vector_index.close()
        await self.llm.close()

    async def _resolve(self, observation, related: list[MemoryRecord]) -> MemoryChange:
        prompt = (
            "New observation:\n" + json.dumps(observation.as_dict(), ensure_ascii=False) +
            "\nRelated memories:\n" + json.dumps([item.as_dict() for item in related], ensure_ascii=False) +
            "\nReturn {\"action\":\"ADD|UPDATE|DELETE|NONE\",\"memory_id\":str,"
            "\"content\":str,\"reason\":str}."
        )
        data = await self.llm.call_json(system_prompt=MEMORY_RESOLUTION_SYSTEM, prompt=prompt)
        return MemoryChange(
            action=MemoryChangeAction(str(data.get("action", "NONE")).upper()),
            observation_id=observation.id, memory_id=str(data.get("memory_id") or ""),
            content=str(data.get("content") or observation.content), reason=str(data.get("reason") or ""),
        )

    async def _apply_change(self, scope, observation, change, related) -> None:
        if change.action == MemoryChangeAction.NONE:
            return
        if change.action == MemoryChangeAction.DELETE:
            if change.memory_id:
                await self.delete(change.memory_id, scope)
            return
        existing = await self.archive.get_memory(change.memory_id, scope) if change.memory_id else None
        memory_id = change.memory_id if existing else observation.id
        content = change.content or observation.content
        record = MemoryRecord(
            id=memory_id, content=content, kind=observation.kind,
            importance=observation.importance, provider=self.name, scope=scope,
            created_at=existing.created_at if existing else observation.created_at,
            metadata={"index_status": "pending"},
        )
        await self.archive.upsert_memory(
            scope, record, action=change.action.value,
            previous_content=existing.content if existing else "", reason=change.reason,
        )
        try:
            vector = (await self.embedding.embed([content]))[0]
            await self.vector_index.upsert(memory_id, vector, {
                "user_id": scope.user_id, "profile": scope.profile,
                "kind": observation.kind.value, "content": content,
            })
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return
        await self.archive.set_memory_index_status(memory_id, "ready")
        self.last_error = ""


def reciprocal_rank_fusion(semantic_ids: list[str], keyword_ids: list[str], *, k: int = 60,
                           semantic_weight: float = 0.6, keyword_weight: float = 0.4) -> dict[str, float]:
    scores: dict[str, float] = {}
    for rank, memory_id in enumerate(semantic_ids, start=1):
        scores[memory_id] = scores.get(memory_id, 0) + semantic_weight / (k + rank)
    for rank, memory_id in enumerate(keyword_ids, start=1):
        scores[memory_id] = scores.get(memory_id, 0) + keyword_weight / (k + rank)
    return scores
