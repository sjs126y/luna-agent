"""MemoryManager core orchestration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from luna_agent.memory.internal import InternalMemoryStore
from luna_agent.memory.manager import MemoryManager
from luna_agent.memory.models import MemoryRecord, MemoryReviewResult


class Router:
    requested_provider = "luna"
    effective_provider = "fallback"
    fallback_reason = "qdrant unavailable"

    async def search(self, query, scope, *, limit=5):
        return [MemoryRecord(id="m1", content="prefers Chinese", provider="fallback", scope=scope)]

    async def list(self, scope, *, limit=100):
        return await self.search("", scope, limit=limit)

    async def delete(self, memory_id, scope):
        return memory_id == "m1"

    async def review(self, messages, scope):
        return MemoryReviewResult(provider="fallback")

    async def maybe_recover(self, scope):
        return False

    async def history(self, memory_id):
        return []

    async def migrate(self, observations, scope):
        return MemoryReviewResult(observations=observations, provider="fallback")

    def health_snapshot(self, scope=None):
        return {
            "requested_provider": self.requested_provider,
            "effective_provider": self.effective_provider,
            "fallback_reason": self.fallback_reason,
            "provider": {"available": True},
        }

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_memory_manager_uses_profile_snapshot_and_external_router(tmp_path) -> None:
    system = tmp_path / "system"
    profile = system / "work"
    profile.mkdir(parents=True)
    (profile / "USER.md").write_text("Prefers concise answers", encoding="utf-8")
    internal = InternalMemoryStore(system, profile_map={"cli:work:u1": "work"})
    archive = SimpleNamespace(pending_buffer_count=_zero)
    manager = MemoryManager(internal=internal, router=Router(), archive=archive)

    snapshot = manager.get_internal_snapshot("cli:work:u1")
    injected = await manager.prefetch("language", session_key="cli:work:u1")
    health = await manager.health_snapshot()

    assert "Prefers concise answers" in snapshot.content
    assert "prefers Chinese" in injected[0]["content"][0]["text"]
    records = await manager.search_entries("language", session_key="cli:work:u1")
    assert records[0]["source_provider"] == "fallback"
    assert records[0]["effective_provider"] == "fallback"
    assert health["requested_provider"] == "luna"
    assert health["effective_provider"] == "fallback"
    assert health["fallback_reason"] == "qdrant unavailable"


async def _zero(scope):
    return 0
