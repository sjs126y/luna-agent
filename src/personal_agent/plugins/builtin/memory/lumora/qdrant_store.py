"""Qdrant vector index adapter isolated from memory domain objects."""

from __future__ import annotations

from typing import Any
from uuid import UUID
from dataclasses import dataclass
from pathlib import Path

from personal_agent.memory.models import MemoryRecord, MemoryScope
from personal_agent.plugins.builtin.memory.lumora.backends.base import BackendHealth, SearchHit


_FILTER_PAYLOAD_INDEXES = ("user_id", "profile")


@dataclass(frozen=True)
class QdrantVectorConfig:
    collection: str
    url: str = ""
    path: str = ""
    api_key: str = ""
    timeout_seconds: float = 10.0


class QdrantMemoryIndex:
    name = "qdrant"

    def __init__(self, config: QdrantVectorConfig, *, dimensions: int = 0, client=None) -> None:
        self._local = bool(config.path)
        if client is None:
            from qdrant_client import AsyncQdrantClient

            if self._local:
                Path(config.path).parent.mkdir(parents=True, exist_ok=True)
                client = AsyncQdrantClient(path=config.path)
            else:
                client = AsyncQdrantClient(
                    url=config.url, api_key=config.api_key or None, timeout=config.timeout_seconds
                )
        self.client = client
        self.config = config
        self.collection = config.collection
        self.dimensions = dimensions
        self._ready = False
        self.last_error = ""

    async def initialize(self, dimensions: int) -> None:
        await self.ensure_collection(dimensions)

    async def ensure_collection(self, dimensions: int) -> None:
        if self._ready:
            if self.dimensions != dimensions:
                raise RuntimeError("Qdrant collection dimension mismatch")
            return
        from qdrant_client.models import Distance, VectorParams

        exists = await self.client.collection_exists(self.collection)
        payload_schema: dict[str, Any] = {}
        if not exists:
            await self.client.create_collection(
                self.collection, vectors_config=VectorParams(size=dimensions, distance=Distance.COSINE)
            )
        else:
            info = await self.client.get_collection(self.collection)
            configured = int(info.config.params.vectors.size)
            if configured != dimensions:
                raise RuntimeError(
                    f"Qdrant collection dimension mismatch: expected {configured}, got {dimensions}"
                )
            payload_schema = dict(getattr(info, "payload_schema", {}) or {})
        if not self._local:
            await self._ensure_filter_indexes(payload_schema)
        self.dimensions = dimensions
        self._ready = True

    async def _ensure_filter_indexes(self, payload_schema: dict[str, Any]) -> None:
        for field_name in _FILTER_PAYLOAD_INDEXES:
            current = payload_schema.get(field_name)
            if current is not None:
                current_type = _payload_schema_type(current)
                if current_type != "keyword":
                    raise RuntimeError(
                        f"Qdrant payload index mismatch for {field_name}: "
                        f"expected keyword, got {current_type or 'unknown'}"
                    )
                continue
            await self.client.create_payload_index(
                collection_name=self.collection,
                field_name=field_name,
                field_schema="keyword",
                wait=True,
            )

    async def upsert(self, memory: MemoryRecord, vector: list[float]) -> None:
        from qdrant_client.models import PointStruct

        await self.ensure_collection(len(vector))
        scope = memory.scope or MemoryScope(user_id="")
        payload = {
            "user_id": scope.user_id,
            "profile": scope.profile,
            "kind": memory.kind.value,
            "content": memory.content,
        }
        await self.client.upsert(
            self.collection,
            points=[PointStruct(id=memory.id, vector=vector, payload=payload)],
            wait=True,
        )

    async def search(
        self,
        vector: list[float],
        scope: MemoryScope,
        *,
        limit: int,
    ) -> list[SearchHit]:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        await self.ensure_collection(len(vector))
        query_filter = Filter(must=[
            FieldCondition(key="user_id", match=MatchValue(value=scope.user_id)),
            FieldCondition(key="profile", match=MatchValue(value=scope.profile)),
        ])
        if hasattr(self.client, "query_points"):
            result = await self.client.query_points(
                self.collection, query=vector, query_filter=query_filter, limit=limit
            )
            points = result.points
        else:
            points = await self.client.search(
                self.collection, query_vector=vector, query_filter=query_filter, limit=limit
            )
        return [
            SearchHit(
                memory_id=_canonical_memory_id(point.id),
                source="semantic",
                rank=rank,
                score=float(point.score),
            )
            for rank, point in enumerate(points, start=1)
        ]

    async def delete(self, memory_id: str) -> None:
        from qdrant_client.models import PointIdsList

        await self.client.delete(self.collection, points_selector=PointIdsList(points=[memory_id]), wait=True)

    def fingerprint(self) -> str:
        location = str(Path(self.config.path).resolve()) if self._local else self.config.url
        return f"{self.name}:{'local' if self._local else 'remote'}:{location}:{self.collection}"

    def health_snapshot(self) -> BackendHealth:
        return BackendHealth(self.name, "failed" if self.last_error else "ready", self.last_error)

    async def close(self) -> None:
        await self.client.close()


def _payload_schema_type(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("data_type") or value.get("type") or ""
    else:
        value = getattr(value, "data_type", value)
    value = getattr(value, "value", value)
    return str(value or "").strip().lower()


def _canonical_memory_id(value: Any) -> str:
    text = str(value)
    try:
        return UUID(text).hex
    except (AttributeError, TypeError, ValueError):
        return text
