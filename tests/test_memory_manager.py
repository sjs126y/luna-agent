"""Memory manager diagnostics and entry management."""

from __future__ import annotations

import numpy as np
import pytest

from personal_agent.memory.manager import MemoryManager
from personal_agent.plugins.builtin.memory.embedding.provider import EmbeddingMemoryProvider
from personal_agent.plugins.builtin.memory.file.provider import FileMemoryProvider


@pytest.mark.asyncio
async def test_file_memory_provider_lists_searches_and_deletes_entries(tmp_path):
    provider = FileMemoryProvider(tmp_path)
    await provider.save("remember project")
    await provider.save_user("prefers Chinese")

    entries = await provider.list_entries()
    search = await provider.search_entries("Chinese")

    assert [entry["id"] for entry in entries] == ["memory:1", "user:1"]
    assert search[0]["target"] == "user"

    assert await provider.delete("user:1", target="all") is True
    assert await provider.delete("user:99", target="user") is False
    assert [entry["id"] for entry in await provider.list_entries()] == ["memory:1"]


@pytest.mark.asyncio
async def test_embedding_memory_provider_lists_and_deletes_without_loading_model(tmp_path):
    provider = EmbeddingMemoryProvider(tmp_path)
    provider._texts = [
        {"id": "abc", "text": "first", "created_at": "2026-01-01T00:00:00"},
        {"id": "def", "text": "second", "created_at": "2026-01-02T00:00:00"},
    ]
    provider._embeddings = np.ones((2, 3), dtype=np.float32)

    entries = await provider.list_entries()
    deleted = await provider.delete("abc")

    assert entries[0]["id"] == "abc"
    assert deleted is True
    assert [entry["id"] for entry in await provider.list_entries()] == ["def"]
    assert provider._embeddings.shape == (1, 3)
    assert provider.health_snapshot()["model_loaded"] is False


@pytest.mark.asyncio
async def test_memory_manager_health_and_lookup(tmp_path):
    provider = FileMemoryProvider(tmp_path)
    await provider.save("alpha memory")
    manager = MemoryManager(provider)

    entries = await manager.list_entries(target="memory")
    found = await manager.get_entry("memory:1", target="memory")
    health = await manager.health_snapshot()

    assert entries[0]["provider"] == "builtin"
    assert found["text"] == "alpha memory"
    assert health["builtin_available"] is True
    assert health["providers"]["builtin"]["entries"] == 1
