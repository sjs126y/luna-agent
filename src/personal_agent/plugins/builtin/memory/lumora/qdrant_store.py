"""Qdrant vector index adapter isolated from memory domain objects."""

from __future__ import annotations

from typing import Any


_FILTER_PAYLOAD_INDEXES = ("user_id", "profile")


class QdrantMemoryIndex:
    def __init__(self, config, *, dimensions: int = 0, client=None) -> None:
        if client is None:
            from qdrant_client import AsyncQdrantClient

            client = AsyncQdrantClient(
                url=config.url, api_key=config.api_key or None, timeout=config.timeout_seconds
            )
        self.client = client
        self.collection = config.collection
        self.dimensions = dimensions
        self._ready = False

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

    async def upsert(self, memory_id: str, vector: list[float], payload: dict[str, Any]) -> None:
        from qdrant_client.models import PointStruct

        await self.ensure_collection(len(vector))
        await self.client.upsert(
            self.collection,
            points=[PointStruct(id=memory_id, vector=vector, payload=payload)],
            wait=True,
        )

    async def search(self, vector: list[float], *, user_id: str, profile: str, limit: int) -> list[tuple[str, float]]:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        await self.ensure_collection(len(vector))
        query_filter = Filter(must=[
            FieldCondition(key="user_id", match=MatchValue(value=user_id)),
            FieldCondition(key="profile", match=MatchValue(value=profile)),
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
        return [(str(point.id), float(point.score)) for point in points]

    async def delete(self, memory_id: str) -> None:
        from qdrant_client.models import PointIdsList

        await self.client.delete(self.collection, points_selector=PointIdsList(points=[memory_id]), wait=True)

    async def close(self) -> None:
        await self.client.close()


def _payload_schema_type(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("data_type") or value.get("type") or ""
    else:
        value = getattr(value, "data_type", value)
    value = getattr(value, "value", value)
    return str(value or "").strip().lower()
