"""Thin normalization adapter over the official Mem0 Python API."""

from __future__ import annotations

import asyncio
from typing import Any

from personal_agent.memory.external.base import ExternalMemoryProvider
from personal_agent.memory.models import (
    MemoryChange, MemoryChangeAction, MemoryRecord, MemoryReviewResult, MemoryScope,
    Observation, ObservationKind,
)


class Mem0MemoryProvider(ExternalMemoryProvider):
    name = "mem0"

    def __init__(self, *, context, archive, client=None) -> None:
        if client is None:
            from mem0 import Memory

            config = dict(context.provider_options)
            config.setdefault("llm", {"provider": context.llm.provider, "config": {
                "model": context.llm.model, "api_key": context.llm.api_key,
                "base_url": context.llm.base_url,
            }})
            client = Memory.from_config(config)
        self.client = client
        self.archive = archive
        self.last_error = ""

    async def review(self, messages: list[dict[str, Any]], scope: MemoryScope) -> MemoryReviewResult:
        result = await asyncio.to_thread(
            self.client.add, messages, user_id=scope.user_id, agent_id=scope.agent_id,
            run_id=scope.session_key or None,
        )
        values = result.get("results", result if isinstance(result, list) else [])
        observations: list[Observation] = []
        changes: list[MemoryChange] = []
        for item in values:
            content = str(item.get("memory") or item.get("text") or "").strip()
            if not content:
                continue
            observation = Observation(kind=ObservationKind.FACT, content=content)
            observations.append(observation)
            action = MemoryChangeAction(str(item.get("event") or "ADD").upper())
            changes.append(MemoryChange(
                action=action, observation_id=observation.id,
                memory_id=str(item.get("id") or ""), content=content,
            ))
        await self.archive.save_observations(scope, tuple(observations))
        return MemoryReviewResult(tuple(observations), tuple(changes), self.name)

    async def search(self, query: str, scope: MemoryScope, *, limit: int = 5) -> list[MemoryRecord]:
        result = await asyncio.to_thread(
            self.client.search, query, user_id=scope.user_id, agent_id=scope.agent_id, limit=limit
        )
        values = result.get("results", result if isinstance(result, list) else [])
        return [_record(item, scope) for item in values[:limit]]

    async def list(self, scope: MemoryScope, *, limit: int = 100) -> list[MemoryRecord]:
        result = await asyncio.to_thread(self.client.get_all, user_id=scope.user_id, agent_id=scope.agent_id)
        values = result.get("results", result if isinstance(result, list) else [])
        return [_record(item, scope) for item in values[:limit]]

    async def delete(self, memory_id: str, scope: MemoryScope) -> bool:
        await asyncio.to_thread(self.client.delete, memory_id)
        return True

    async def history(self, memory_id: str) -> list[MemoryChange]:
        values = await asyncio.to_thread(self.client.history, memory_id)
        return [MemoryChange(
            action=MemoryChangeAction(str(item.get("event") or "NONE").upper()),
            observation_id="", memory_id=memory_id,
            content=str(item.get("new_memory") or item.get("memory") or ""),
            previous_content=str(item.get("old_memory") or ""),
        ) for item in values]

    async def migrate(self, observations, scope: MemoryScope) -> MemoryReviewResult:
        messages = [
            {"role": "user", "content": item.content}
            for item in observations
        ]
        result = await asyncio.to_thread(
            self.client.add, messages, user_id=scope.user_id, agent_id=scope.agent_id,
            run_id=scope.session_key or None,
        )
        values = result.get("results", result if isinstance(result, list) else [])
        changes = tuple(MemoryChange(
            action=MemoryChangeAction(str(item.get("event") or "ADD").upper()),
            observation_id="", memory_id=str(item.get("id") or ""),
            content=str(item.get("memory") or ""),
        ) for item in values)
        return MemoryReviewResult(tuple(observations), changes, self.name)

    def health_snapshot(self) -> dict[str, Any]:
        return {"provider": self.name, "available": not self.last_error, "last_error": self.last_error}


def _record(item: dict[str, Any], scope: MemoryScope) -> MemoryRecord:
    return MemoryRecord(
        id=str(item.get("id") or ""), content=str(item.get("memory") or item.get("text") or ""),
        provider="mem0", scope=scope, score=float(item.get("score") or 0),
    )
