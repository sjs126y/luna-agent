import pytest

from personal_agent.memory.archive import MemoryArchive
from personal_agent.memory.models import MemoryRecord, MemoryScope, Observation, ObservationKind


@pytest.mark.asyncio
async def test_memory_archive_persists_records_buffer_and_checkpoint(tmp_path) -> None:
    archive = MemoryArchive(tmp_path / "memory.db")
    await archive.initialize()
    scope = MemoryScope(user_id="u1", profile="default")
    observation = Observation(kind=ObservationKind.FACT, content="The project is Lumora")
    batch_id = await archive.create_review_batch(scope, requested="lumora", effective="fallback")
    await archive.save_observations(scope, (observation,), batch_id=batch_id)
    assert await archive.add_to_internal_buffer(scope, (observation,)) == 1
    assert await archive.pending_buffer_count(scope) == 1
    record = MemoryRecord(id="m1", content="The project is Lumora", provider="fallback", scope=scope)
    await archive.upsert_memory(scope, record)
    assert [item.id for item in await archive.search_bm25(scope, "Lumora")] == ["m1"]
    await archive.set_checkpoint(scope, last_turn_id="t1", reviewed_turns=10)
    assert (await archive.get_checkpoint(scope))["last_turn_id"] == "t1"
    assert await archive.next_internal_revision("default", {"USER.md": "abc"}) == 1
    assert await archive.next_internal_revision("default", {"USER.md": "def"}) == 2
    await archive.close()
