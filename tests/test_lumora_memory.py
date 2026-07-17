from types import SimpleNamespace

import pytest

from personal_agent.memory.archive import MemoryArchive
from personal_agent.memory.models import MemoryRecord, MemoryScope, Observation, ObservationKind
from personal_agent.plugins.builtin.memory.lumora.backends.base import BackendHealth
from personal_agent.plugins.builtin.memory.lumora.backends.config import RetrievalConfig
from personal_agent.plugins.builtin.memory.lumora.backends.keyword import SqliteFts5KeywordIndex
from personal_agent.plugins.builtin.memory.lumora.backends.ranking import NoOpReranker, WeightedRrfFusion
from personal_agent.plugins.builtin.memory.lumora.provider import LumoraMemoryProvider
from personal_agent.plugins.builtin.memory.lumora.qdrant_store import QdrantMemoryIndex, QdrantVectorConfig


class _FakeQdrantClient:
    def __init__(self, *, exists: bool, payload_schema=None, dimensions: int = 1024) -> None:
        self.exists = exists
        self.payload_schema = payload_schema or {}
        self.dimensions = dimensions
        self.created_collection = False
        self.created_indexes: list[tuple[str, str, str, bool]] = []
        self.query_result = SimpleNamespace(points=[])

    async def collection_exists(self, collection: str) -> bool:
        return self.exists

    async def create_collection(self, collection: str, *, vectors_config) -> None:
        self.created_collection = True

    async def get_collection(self, collection: str):
        return SimpleNamespace(
            config=SimpleNamespace(params=SimpleNamespace(vectors=SimpleNamespace(size=self.dimensions))),
            payload_schema=self.payload_schema,
        )

    async def create_payload_index(
        self, *, collection_name: str, field_name: str, field_schema: str, wait: bool
    ) -> None:
        self.created_indexes.append((collection_name, field_name, field_schema, wait))

    async def query_points(self, collection: str, *, query, query_filter, limit: int):
        return self.query_result


@pytest.mark.asyncio
async def test_qdrant_index_creates_filter_indexes_for_existing_collection() -> None:
    client = _FakeQdrantClient(exists=True)
    index = QdrantMemoryIndex(
        QdrantVectorConfig(collection="lumora_memories", url="http://qdrant"), dimensions=1024, client=client
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
        QdrantVectorConfig(collection="lumora_memories", url="http://qdrant"), dimensions=1024, client=client
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
        QdrantVectorConfig(collection="lumora_memories", url="http://qdrant"), dimensions=1024, client=client
    )

    with pytest.raises(RuntimeError, match="user_id: expected keyword, got integer"):
        await index.ensure_collection(1024)


@pytest.mark.asyncio
async def test_qdrant_search_normalizes_uuid_ids_to_archive_format() -> None:
    client = _FakeQdrantClient(
        exists=True,
        payload_schema={"user_id": "keyword", "profile": "keyword"},
    )
    client.query_result = SimpleNamespace(points=[
        SimpleNamespace(id="4b4915da-452f-440d-b750-c127856e2ace", score=0.9),
        SimpleNamespace(id="legacy-id", score=0.5),
    ])
    index = QdrantMemoryIndex(
        QdrantVectorConfig(collection="lumora_memories", url="http://qdrant"), dimensions=1024, client=client
    )

    result = await index.search([0.1] * 1024, MemoryScope(user_id="u1"), limit=5)

    assert [(item.memory_id, item.score) for item in result] == [
        ("4b4915da452f440db750c127856e2ace", 0.9), ("legacy-id", 0.5),
    ]


@pytest.mark.asyncio
async def test_qdrant_local_persists_and_filters_by_scope(tmp_path) -> None:
    pytest.importorskip("qdrant_client")
    config = QdrantVectorConfig(
        collection="lumora_memories",
        path=str(tmp_path / "qdrant"),
    )
    scope = MemoryScope(user_id="u1", profile="default")
    other_scope = MemoryScope(user_id="u2", profile="default")
    memory = MemoryRecord(
        id="4b4915da452f440db750c127856e2ace",
        content="local vector memory",
        scope=scope,
    )
    index = QdrantMemoryIndex(config, dimensions=2)
    await index.upsert(memory, [1.0, 0.0])
    await index.close()

    reopened = QdrantMemoryIndex(config, dimensions=2)
    found = await reopened.search([1.0, 0.0], scope, limit=5)
    hidden = await reopened.search([1.0, 0.0], other_scope, limit=5)

    assert [item.memory_id for item in found] == [memory.id]
    assert hidden == []
    await reopened.close()


class _Embedding:
    name = "fake_embedding"
    dimensions = 2

    async def embed(self, texts):
        return [[0.1, 0.2] for _ in texts]

    async def close(self):
        pass

    def fingerprint(self):
        return "fake:2"

    def health_snapshot(self):
        return BackendHealth(self.name)


class _VectorIndex:
    name = "fake_vector"

    async def search(self, vector, scope, *, limit):
        return []

    async def upsert(self, memory, vector):
        pass

    async def delete(self, memory_id):
        pass

    def fingerprint(self):
        return "fake"

    def health_snapshot(self):
        return BackendHealth(self.name)

    async def close(self):
        pass


class _FailingVectorIndex(_VectorIndex):
    async def search(self, vector, scope, *, limit):
        raise TimeoutError("vector unavailable")


class _ResolutionLLM:
    async def call_json(self, *, system_prompt, prompt):
        return {"action": "ADD", "memory_id": "", "content": "stored content", "reason": "new"}

    async def close(self):
        pass


def _provider(archive, *, vector_index=None):
    return LumoraMemoryProvider(
        archive=archive,
        context=SimpleNamespace(),
        embedding=_Embedding(),
        vector_index=vector_index or _VectorIndex(),
        keyword_index=SqliteFts5KeywordIndex(archive),
        fusion=WeightedRrfFusion(),
        reranker=NoOpReranker(),
        retrieval_config=RetrievalConfig(),
        llm=_ResolutionLLM(),
    )


@pytest.mark.asyncio
async def test_lumora_migrate_returns_applied_memory_id(tmp_path) -> None:
    archive = MemoryArchive(tmp_path / "memory.db")
    await archive.initialize()
    provider = _provider(archive)
    observation = Observation(kind=ObservationKind.EVENT, content="new observation")
    scope = MemoryScope(user_id="u1")

    result = await provider.migrate((observation,), scope)

    assert result.provider == "lumora"
    assert result.changes[0].memory_id == observation.id
    assert result.changes[0].content == "stored content"
    stored = await archive.get_memory(observation.id, scope)
    assert stored is not None
    assert stored.provider == "lumora"
    assert stored.metadata["index_status"] == "ready"
    await archive.close()


@pytest.mark.asyncio
async def test_lumora_reindexes_pending_records(tmp_path) -> None:
    archive = MemoryArchive(tmp_path / "memory.db")
    await archive.initialize()
    scope = MemoryScope(user_id="u1")
    record = MemoryRecord(
        id="m1",
        content="repair vector",
        provider="lumora",
        scope=scope,
        metadata={"index_status": "pending"},
    )
    await archive.upsert_memory(scope, record)
    provider = _provider(archive)

    result = await provider.reindex([record], scope)

    assert result == {"attempted": 1, "completed": 1, "failed": 0}
    stored = await archive.get_memory("m1", scope)
    assert stored.metadata["index_status"] == "ready"
    await archive.close()


@pytest.mark.asyncio
async def test_lumora_search_recalls_uuid_returned_by_qdrant(tmp_path) -> None:
    archive = MemoryArchive(tmp_path / "memory.db")
    await archive.initialize()
    scope = MemoryScope(user_id="u1")
    memory_id = "4b4915da452f440db750c127856e2ace"
    record = MemoryRecord(
        id=memory_id,
        content="用户喜欢爵士乐",
        provider="lumora",
        scope=scope,
    )
    await archive.upsert_memory(scope, record)
    qdrant = _FakeQdrantClient(
        exists=True,
        payload_schema={"user_id": "keyword", "profile": "keyword"},
        dimensions=2,
    )
    qdrant.query_result = SimpleNamespace(points=[
        SimpleNamespace(id="4b4915da-452f-440d-b750-c127856e2ace", score=0.9),
    ])
    provider = _provider(
        archive,
        vector_index=QdrantMemoryIndex(
            QdrantVectorConfig(collection="lumora_memories", url="http://qdrant"),
            dimensions=2,
            client=qdrant,
        ),
    )

    result = await provider.search("音乐偏好", scope)

    assert [item.id for item in result] == [memory_id]
    assert result[0].content == "用户喜欢爵士乐"
    await archive.close()


@pytest.mark.asyncio
async def test_lumora_search_degrades_to_keyword_when_vector_fails(tmp_path) -> None:
    archive = MemoryArchive(tmp_path / "memory.db")
    await archive.initialize()
    scope = MemoryScope(user_id="u1")
    record = MemoryRecord(id="m1", content="prefers jazz music", scope=scope)
    await archive.upsert_memory(scope, record)
    provider = _provider(archive, vector_index=_FailingVectorIndex())

    result = await provider.search("jazz", scope)

    assert [item.id for item in result] == ["m1"]
    assert provider.health_snapshot()["status"] == "degraded"
    assert provider.health_snapshot()["components"]["semantic"]["status"] == "degraded"
    await provider.close()
    await archive.close()
