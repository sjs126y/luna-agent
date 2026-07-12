from types import SimpleNamespace

import pytest

from personal_agent.plugins.builtin.memory.lumora.provider import reciprocal_rank_fusion
from personal_agent.plugins.builtin.memory.lumora.qdrant_store import QdrantMemoryIndex


def test_reciprocal_rank_fusion_combines_semantic_and_bm25() -> None:
    scores = reciprocal_rank_fusion(["semantic", "both"], ["both", "keyword"])

    assert scores["both"] > scores["semantic"]
    assert scores["semantic"] > scores["keyword"]


class _FakeQdrantClient:
    def __init__(self, *, exists: bool, payload_schema=None) -> None:
        self.exists = exists
        self.payload_schema = payload_schema or {}
        self.created_collection = False
        self.created_indexes: list[tuple[str, str, str, bool]] = []

    async def collection_exists(self, collection: str) -> bool:
        return self.exists

    async def create_collection(self, collection: str, *, vectors_config) -> None:
        self.created_collection = True

    async def get_collection(self, collection: str):
        return SimpleNamespace(
            config=SimpleNamespace(params=SimpleNamespace(vectors=SimpleNamespace(size=1024))),
            payload_schema=self.payload_schema,
        )

    async def create_payload_index(
        self, *, collection_name: str, field_name: str, field_schema: str, wait: bool
    ) -> None:
        self.created_indexes.append((collection_name, field_name, field_schema, wait))


@pytest.mark.asyncio
async def test_qdrant_index_creates_filter_indexes_for_existing_collection() -> None:
    client = _FakeQdrantClient(exists=True)
    index = QdrantMemoryIndex(
        SimpleNamespace(collection="lumora_memories"), dimensions=1024, client=client
    )

    await index.ensure_collection(1024)

    assert client.created_collection is False
    assert client.created_indexes == [
        ("lumora_memories", "user_id", "keyword", True),
        ("lumora_memories", "profile", "keyword", True),
    ]


@pytest.mark.asyncio
async def test_qdrant_index_keeps_existing_keyword_indexes() -> None:
    keyword = SimpleNamespace(data_type=SimpleNamespace(value="keyword"))
    client = _FakeQdrantClient(
        exists=True,
        payload_schema={"user_id": keyword, "profile": keyword},
    )
    index = QdrantMemoryIndex(
        SimpleNamespace(collection="lumora_memories"), dimensions=1024, client=client
    )

    await index.ensure_collection(1024)

    assert client.created_indexes == []


@pytest.mark.asyncio
async def test_qdrant_index_rejects_wrong_payload_index_type() -> None:
    client = _FakeQdrantClient(
        exists=True,
        payload_schema={"user_id": {"data_type": "integer"}},
    )
    index = QdrantMemoryIndex(
        SimpleNamespace(collection="lumora_memories"), dimensions=1024, client=client
    )

    with pytest.raises(RuntimeError, match="user_id: expected keyword, got integer"):
        await index.ensure_collection(1024)
