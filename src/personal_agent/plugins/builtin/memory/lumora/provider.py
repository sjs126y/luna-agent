"""Lumora long-term memory with LLM resolution and hybrid retrieval."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import logging
from time import monotonic
from typing import Any

from personal_agent.memory.external.base import ExternalMemoryProvider
from personal_agent.memory.llm import MemoryLLMFacade
from personal_agent.memory.models import (
    MemoryChange, MemoryChangeAction, MemoryRecord, MemoryReviewResult, MemoryScope,
)
from personal_agent.memory.prompts import MEMORY_RESOLUTION_SYSTEM

logger = logging.getLogger(__name__)


class LumoraMemoryProvider(ExternalMemoryProvider):
    name = "lumora"

    def __init__(
        self,
        *,
        archive,
        context,
        embedding,
        vector_index,
        keyword_index,
        fusion,
        reranker,
        retrieval_config,
        llm=None,
    ) -> None:
        self.archive = archive
        self.context = context
        self.embedding = embedding
        self.vector_index = vector_index
        self.keyword_index = keyword_index
        self.fusion = fusion
        self.reranker = reranker
        self.retrieval_config = retrieval_config
        self.llm = llm or MemoryLLMFacade(context.llm)
        self.last_error = ""
        self._component_errors: dict[str, str] = {}

    async def review(self, messages: list[dict[str, Any]], scope: MemoryScope) -> MemoryReviewResult:
        batch_id = await self.archive.create_review_batch(scope, requested=self.name, effective=self.name)
        observations = await self.llm.extract_observations(messages)
        await self.archive.save_observations(scope, observations, batch_id=batch_id)
        changes: list[MemoryChange] = []
        for observation in observations:
            related = await self.search(observation.content, scope, limit=5)
            change = await self._resolve(observation, related)
            changes.append(await self._apply_change(scope, observation, change, related))
        await self.archive.finish_review_batch(batch_id, status="completed")
        return MemoryReviewResult(observations, tuple(changes), self.name, batch_id)

    async def search(self, query: str, scope: MemoryScope, *, limit: int = 5) -> list[MemoryRecord]:
        candidate_limit = max(limit * 3, 10)
        semantic_result, keyword_result = await asyncio.gather(
            asyncio.wait_for(
                self._semantic_search(query, scope, candidate_limit),
                timeout=self.retrieval_config.semantic_timeout_seconds,
            ),
            asyncio.wait_for(
                self._keyword_search(query, scope, candidate_limit),
                timeout=self.retrieval_config.keyword_timeout_seconds,
            ),
            return_exceptions=True,
        )
        result_sets: dict[str, list] = {}
        errors: dict[str, BaseException] = {}
        for source, result in (("semantic", semantic_result), ("keyword", keyword_result)):
            if isinstance(result, BaseException):
                errors[source] = result
                self._component_errors[source] = f"{type(result).__name__}: {result}"
            else:
                result_sets[source] = result
                self._component_errors.pop(source, None)
        if not result_sets:
            detail = "; ".join(f"{name}: {type(exc).__name__}: {exc}" for name, exc in errors.items())
            self.last_error = detail
            raise RuntimeError(f"Lumora retrieval backends failed: {detail}")

        memory_ids = list(dict.fromkeys(
            hit.memory_id for hits in result_sets.values() for hit in hits
        ))
        records = await self.archive.get_memories(memory_ids, scope)
        by_id = {item.id: item for item in records}
        fused = await self.fusion.fuse(query, by_id, result_sets, limit=candidate_limit)
        try:
            ranked = await asyncio.wait_for(
                self.reranker.rerank(query, fused, by_id, limit=limit),
                timeout=self.retrieval_config.reranker_timeout_seconds,
            )
            self._component_errors.pop("reranker", None)
        except Exception as exc:
            self._component_errors["reranker"] = f"{type(exc).__name__}: {exc}"
            logger.warning("Lumora reranker degraded to fusion results: %s", self._component_errors["reranker"])
            ranked = fused[:limit]
        self.last_error = ""
        return [
            replace(
                by_id[item.memory_id],
                score=item.score,
                metadata={
                    **by_id[item.memory_id].metadata,
                    **item.metadata,
                    "retrieval_sources": list(item.sources),
                },
            )
            for item in ranked
            if item.memory_id in by_id
        ]

    async def list(self, scope: MemoryScope, *, limit: int = 100) -> list[MemoryRecord]:
        return await self.archive.list_memories(scope, limit=limit)

    async def delete(self, memory_id: str, scope: MemoryScope) -> bool:
        deleted = await self.archive.delete_memory(memory_id, scope, provider=self.name)
        if deleted:
            await self.keyword_index.delete(memory_id)
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
            started = monotonic()
            related = await self.search(observation.content, scope, limit=5)
            searched_at = monotonic()
            change = await self._resolve(observation, related)
            resolved_at = monotonic()
            applied = await self._apply_change(scope, observation, change, related)
            finished_at = monotonic()
            changes.append(applied)
            logger.info(
                "Lumora memory migrate: action=%s search=%.3fs resolve=%.3fs apply=%.3fs total=%.3fs",
                applied.action.value,
                searched_at - started,
                resolved_at - searched_at,
                finished_at - resolved_at,
                finished_at - started,
            )
        return MemoryReviewResult(tuple(observations), tuple(changes), self.name)

    async def reindex(self, records: list[MemoryRecord], scope: MemoryScope) -> dict[str, int]:
        result = {"attempted": 0, "completed": 0, "failed": 0}
        for record in records:
            result["attempted"] += 1
            try:
                await self._write_indexes(record)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                await self.archive.mark_memory_index_failed(record.id, self.last_error)
                result["failed"] += 1
                logger.warning("Lumora reindex deferred: %s", self.last_error)
                continue
            await self.archive.mark_memory_index_ready(record.id)
            result["completed"] += 1
            self.last_error = ""
        return result

    async def reindex_all(self, *, index_kind: str = "all", limit: int = 100000) -> dict[str, int]:
        if index_kind not in {"all", "vector", "keyword"}:
            raise ValueError(f"Unsupported memory index kind: {index_kind}")
        records = await self.archive.all_memories(limit=limit)
        indexes = {"vector", "keyword"} if index_kind == "all" else {index_kind}
        failed_ids: set[str] = set()
        if "keyword" in indexes:
            for record in records:
                try:
                    await self._write_indexes(record, indexes={"keyword"})
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    failed_ids.add(record.id)
        if "vector" in indexes:
            for offset in range(0, len(records), 32):
                batch = records[offset:offset + 32]
                try:
                    await self._write_vector_batch(batch)
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    failed_ids.update(record.id for record in batch)
        result = {
            "attempted": len(records),
            "completed": len(records) - len(failed_ids),
            "failed": len(failed_ids),
        }
        if not result["failed"]:
            self.last_error = ""
        return result

    async def _write_vector_batch(self, records: list[MemoryRecord]) -> None:
        if not records:
            return
        vector_fingerprint = self._vector_fingerprint()
        try:
            vectors = await self.embedding.embed([record.content for record in records])
            vector_fingerprint = self._vector_fingerprint()
            await self.archive.ensure_index_backend(
                "vector", self.vector_index.name, vector_fingerprint
            )
            upsert_many = getattr(self.vector_index, "upsert_many", None)
            if upsert_many is not None:
                await upsert_many(records, vectors)
            else:
                for record, vector in zip(records, vectors, strict=True):
                    await self.vector_index.upsert(record, vector)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            for record in records:
                await self.archive.set_backend_index_status(
                    record.id,
                    "vector",
                    backend=self.vector_index.name,
                    fingerprint=vector_fingerprint,
                    status="pending",
                    error=detail,
                )
            raise
        for record in records:
            await self.archive.set_backend_index_status(
                record.id,
                "vector",
                backend=self.vector_index.name,
                fingerprint=vector_fingerprint,
                status="ready",
            )

    async def pending_reindex_records(self, scope: MemoryScope, *, limit: int = 10) -> list[MemoryRecord]:
        return await self.archive.pending_backend_index_memories(scope, limit=limit)

    def health_snapshot(self) -> dict[str, Any]:
        components = {
            "embedding": self.embedding.health_snapshot().as_dict(),
            "vector": self.vector_index.health_snapshot().as_dict(),
            "keyword": self.keyword_index.health_snapshot().as_dict(),
            "fusion": self.fusion.health_snapshot().as_dict(),
            "reranker": self.reranker.health_snapshot().as_dict(),
        }
        for name, detail in self._component_errors.items():
            component = components.setdefault(name, {"provider": name})
            component.update({"status": "degraded", "detail": detail})
        return {
            "provider": self.name,
            "available": not self.last_error,
            "status": "degraded" if self._component_errors else ("failed" if self.last_error else "ready"),
            "last_error": self.last_error,
            "components": components,
            "fingerprints": {
                "embedding": self.embedding.fingerprint(),
                "vector": self._vector_fingerprint(),
                "keyword": self.keyword_index.fingerprint(),
                "fusion": self.fusion.fingerprint(),
                "reranker": self.reranker.fingerprint(),
            },
        }

    async def close(self) -> None:
        await self.embedding.close()
        await self.vector_index.close()
        await self.keyword_index.close()
        await self.fusion.close()
        await self.reranker.close()
        await self.llm.close()

    async def _semantic_search(self, query: str, scope: MemoryScope, limit: int):
        vector = (await self.embedding.embed([query]))[0]
        await self.archive.ensure_index_backend(
            "vector",
            self.vector_index.name,
            self._vector_fingerprint(),
        )
        return await self.vector_index.search(vector, scope, limit=limit)

    async def _keyword_search(self, query: str, scope: MemoryScope, limit: int):
        await self.archive.ensure_index_backend(
            "keyword",
            self.keyword_index.name,
            self.keyword_index.fingerprint(),
            initial_status="ready" if self.keyword_index.name == "sqlite_fts5" else "pending",
        )
        return await self.keyword_index.search(query, scope, limit=limit)

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

    async def _apply_change(self, scope, observation, change, related) -> MemoryChange:
        if change.action == MemoryChangeAction.NONE:
            return change
        if change.action == MemoryChangeAction.DELETE:
            existing = await self.archive.get_memory(change.memory_id, scope) if change.memory_id else None
            if change.memory_id:
                await self.delete(change.memory_id, scope)
            return replace(
                change,
                previous_content=existing.content if existing else change.previous_content,
            )
        existing = await self.archive.get_memory(change.memory_id, scope) if change.memory_id else None
        memory_id = change.memory_id if existing else observation.id
        content = change.content or observation.content
        applied = replace(
            change,
            memory_id=memory_id,
            content=content,
            previous_content=existing.content if existing else "",
        )
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
            await self._write_indexes(record)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            await self.archive.mark_memory_index_failed(memory_id, self.last_error)
            logger.warning("Lumora index write pending: %s", self.last_error)
            return applied
        await self.archive.mark_memory_index_ready(memory_id)
        self.last_error = ""
        return applied

    async def _write_indexes(self, record: MemoryRecord, *, indexes: set[str] | None = None) -> None:
        indexes = indexes or {"vector", "keyword"}
        errors: list[str] = []
        if "keyword" in indexes:
            keyword_fingerprint = self.keyword_index.fingerprint()
            await self.archive.ensure_index_backend(
                "keyword",
                self.keyword_index.name,
                keyword_fingerprint,
                initial_status="ready" if self.keyword_index.name == "sqlite_fts5" else "pending",
            )
            try:
                await self.keyword_index.upsert(record)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                await self.archive.set_backend_index_status(
                    record.id,
                    "keyword",
                    backend=self.keyword_index.name,
                    fingerprint=keyword_fingerprint,
                    status="pending",
                    error=detail,
                )
                errors.append(f"keyword: {detail}")
            else:
                await self.archive.set_backend_index_status(
                    record.id,
                    "keyword",
                    backend=self.keyword_index.name,
                    fingerprint=keyword_fingerprint,
                    status="ready",
                )
        embedded_at = monotonic()
        upserted_at = embedded_at
        if "vector" in indexes:
            try:
                vector = (await self.embedding.embed([record.content]))[0]
                vector_fingerprint = self._vector_fingerprint()
                await self.archive.ensure_index_backend(
                    "vector", self.vector_index.name, vector_fingerprint
                )
                upserted_at = monotonic()
                await self.vector_index.upsert(record, vector)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                vector_fingerprint = self._vector_fingerprint()
                await self.archive.set_backend_index_status(
                    record.id,
                    "vector",
                    backend=self.vector_index.name,
                    fingerprint=vector_fingerprint,
                    status="pending",
                    error=detail,
                )
                errors.append(f"vector: {detail}")
                upserted_at = monotonic()
            else:
                await self.archive.set_backend_index_status(
                    record.id,
                    "vector",
                    backend=self.vector_index.name,
                    fingerprint=vector_fingerprint,
                    status="ready",
                )
        finished_at = monotonic()
        logger.info(
            "Lumora index write: embedding=%.3fs qdrant=%.3fs",
            upserted_at - embedded_at,
            finished_at - upserted_at,
        )
        if errors:
            raise RuntimeError("; ".join(errors))

    def _vector_fingerprint(self) -> str:
        return f"{self.embedding.fingerprint()}|{self.vector_index.fingerprint()}"
