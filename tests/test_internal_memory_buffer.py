from __future__ import annotations

import pytest

from personal_agent.memory.archive import MemoryArchive
from personal_agent.memory.internal import InternalMemoryService, InternalMemoryStore
from personal_agent.memory.models import (
    InternalPatchAction,
    InternalPatchOperation,
    MemoryScope,
    Observation,
    ObservationKind,
)


class Consolidator:
    async def propose(self, *, internal_content, observations):
        return [InternalPatchOperation(
            action=InternalPatchAction.ADD,
            observation_id=observations[0].id,
            target_file="USER.md",
            content=observations[0].content,
        )]


@pytest.mark.asyncio
async def test_internal_buffer_deduplicates_and_consolidates(tmp_path) -> None:
    archive = MemoryArchive(tmp_path / "memory.db")
    await archive.initialize()
    store = InternalMemoryStore(tmp_path / "system")
    service = InternalMemoryService(archive=archive, store=store, consolidator=Consolidator(), buffer_limit=1)
    scope = MemoryScope(user_id="u1")
    first = Observation(kind=ObservationKind.PREFERENCE, content="Prefers concise answers")
    duplicate = Observation(kind=ObservationKind.PREFERENCE, content="  prefers CONCISE answers ")

    assert await service.enqueue(scope, (first,)) == 1
    assert await service.enqueue(scope, (duplicate,)) == 0
    assert await service.should_consolidate(scope) is True
    result = await service.consolidate(scope)

    assert result["applied"] == 1
    assert await archive.pending_buffer_count(scope) == 0
    assert "Prefers concise answers" in (tmp_path / "system" / "USER.md").read_text(encoding="utf-8")
    await archive.close()
