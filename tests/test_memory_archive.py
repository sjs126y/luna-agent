import sqlite3

import pytest

from luna_agent.memory.archive import MemoryArchive
from luna_agent.memory.models import MemoryRecord, MemoryScope, Observation, ObservationKind


@pytest.mark.asyncio
async def test_memory_archive_persists_records_buffer_and_checkpoint(tmp_path) -> None:
    archive = MemoryArchive(tmp_path / "memory.db")
    await archive.initialize()
    scope = MemoryScope(user_id="u1", profile="default")
    observation = Observation(kind=ObservationKind.FACT, content="The project is Luna")
    batch_id = await archive.create_review_batch(scope, requested="luna", effective="fallback")
    await archive.save_observations(scope, (observation,), batch_id=batch_id)
    assert await archive.add_to_internal_buffer(scope, (observation,)) == 1
    assert await archive.pending_buffer_count(scope) == 1
    record = MemoryRecord(id="m1", content="The project is Luna", provider="fallback", scope=scope)
    await archive.upsert_memory(scope, record)
    assert [item.id for item in await archive.search_bm25(scope, "Luna")] == ["m1"]
    await archive.set_checkpoint(scope, last_turn_id="t1", reviewed_turns=10)
    assert (await archive.get_checkpoint(scope))["last_turn_id"] == "t1"
    assert await archive.next_internal_revision("default", {"USER.md": "abc"}) == 1
    assert await archive.next_internal_revision("default", {"USER.md": "def"}) == 2
    await archive.close()


@pytest.mark.asyncio
async def test_memory_archive_migrates_v1_internal_buffer(tmp_path) -> None:
    path = tmp_path / "memory.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
    CREATE TABLE memory_schema (version INTEGER NOT NULL);
    INSERT INTO memory_schema(version) VALUES (1);
    CREATE TABLE internal_buffer (
      observation_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, content_hash TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending', target_file TEXT DEFAULT '', reason TEXT DEFAULT '',
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    """)
    connection.close()

    archive = MemoryArchive(path)
    await archive.initialize()
    cursor = await archive._connection.execute("PRAGMA table_info(internal_buffer)")
    columns = {row["name"] for row in await cursor.fetchall()}

    assert {"proposed_action", "proposed_content", "entry_id"} <= columns
    version = await archive._fetchone("SELECT version FROM memory_schema")
    assert version["version"] == 5
    await archive.close()


@pytest.mark.asyncio
async def test_memory_archive_checkpoints_migration_attempts(tmp_path) -> None:
    archive = MemoryArchive(tmp_path / "memory.db")
    await archive.initialize()
    scope = MemoryScope(user_id="u1")
    observation = Observation(kind=ObservationKind.EVENT, content="pending migration")
    await archive.save_observations(scope, (observation,), migration_status="pending")

    await archive.mark_observation_migration_failed(observation.id, "network reset")
    failed = await archive._fetchone(
        "SELECT migration_status,migration_attempts,migration_error FROM observations WHERE id = ?",
        (observation.id,),
    )
    assert failed["migration_status"] == "pending"
    assert failed["migration_attempts"] == 1
    assert failed["migration_error"] == "network reset"
    assert (await archive.migration_status_counts(scope))["pending"] == 1

    await archive.mark_observations_migrated([observation.id])
    migrated = await archive._fetchone(
        "SELECT migration_status,migration_attempts,migration_error FROM observations WHERE id = ?",
        (observation.id,),
    )
    assert migrated["migration_status"] == "migrated"
    assert migrated["migration_attempts"] == 2
    assert migrated["migration_error"] == ""
    await archive.close()


@pytest.mark.asyncio
async def test_memory_archive_tracks_pending_index_retries(tmp_path) -> None:
    archive = MemoryArchive(tmp_path / "memory.db")
    await archive.initialize()
    scope = MemoryScope(user_id="u1")
    record = MemoryRecord(
        id="m1",
        content="pending vector",
        provider="luna",
        scope=scope,
        metadata={"index_status": "pending"},
    )
    await archive.upsert_memory(scope, record)

    await archive.mark_memory_index_failed("m1", "qdrant timeout")
    failed = await archive._fetchone(
        "SELECT index_status,index_attempts,index_error FROM memories WHERE id = 'm1'"
    )
    assert failed["index_status"] == "pending"
    assert failed["index_attempts"] == 1
    assert failed["index_error"] == "qdrant timeout"
    assert [item.id for item in await archive.pending_index_memories(scope)] == ["m1"]
    assert (await archive.index_status_counts(scope))["pending"] == 1

    await archive.mark_memory_index_ready("m1")
    ready = await archive._fetchone(
        "SELECT index_status,index_attempts,index_error FROM memories WHERE id = 'm1'"
    )
    assert ready["index_status"] == "ready"
    assert ready["index_attempts"] == 2
    assert ready["index_error"] == ""
    await archive.close()


@pytest.mark.asyncio
async def test_memory_archive_tracks_vector_and_keyword_backends_independently(tmp_path) -> None:
    archive = MemoryArchive(tmp_path / "memory.db")
    await archive.initialize()
    scope = MemoryScope(user_id="u1")
    await archive.upsert_memory(scope, MemoryRecord(id="m1", content="indexed memory", scope=scope))

    vector_changed = await archive.ensure_index_backend("vector", "qdrant", "embed:v1|qdrant:local")
    keyword_changed = await archive.ensure_index_backend(
        "keyword", "sqlite_fts5", "sqlite_fts5:unicode61", initial_status="ready"
    )
    await archive.set_backend_index_status(
        "m1",
        "vector",
        backend="qdrant",
        fingerprint="embed:v1|qdrant:local",
        status="pending",
        error="timeout",
    )

    assert vector_changed is False
    assert keyword_changed is False
    assert await archive.backend_index_status(scope) == {
        "keyword": {"ready": 1},
        "vector": {"pending": 1},
    }
    pending = await archive.pending_backend_index_memories(scope)
    assert [item.id for item in pending] == ["m1"]
    metadata = await archive.index_backend_metadata()
    assert metadata["vector"]["backend"] == "qdrant"
    await archive.close()


@pytest.mark.asyncio
async def test_memory_archive_marks_new_generation_pending_on_backend_change(tmp_path) -> None:
    archive = MemoryArchive(tmp_path / "memory.db")
    await archive.initialize()
    scope = MemoryScope(user_id="u1")
    await archive.upsert_memory(scope, MemoryRecord(id="m1", content="indexed memory", scope=scope))
    await archive.ensure_index_backend("vector", "qdrant", "embedding:v1|qdrant:remote")

    changed = await archive.ensure_index_backend("vector", "pgvector", "embedding:v1|pgvector:db")

    assert changed is True
    assert await archive.backend_index_status(scope) == {"vector": {"pending": 1}}
    await archive.close()
