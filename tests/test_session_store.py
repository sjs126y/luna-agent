"""Session store management behavior."""

from __future__ import annotations

import time

import pytest
import pytest_asyncio

from personal_agent.db.database import Database
from personal_agent.gateway.compression_chain import CompressionChain
from personal_agent.gateway.session_store import SessionStore
from personal_agent.models.messages import SessionSource


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "state.db")
    await db.initialize()
    chain = CompressionChain(tmp_path / "compression_chain.json")
    session_store = SessionStore(db, tmp_path, chain=chain)
    await session_store.initialize()
    try:
        yield session_store, db, chain
    finally:
        await db.close()


def _source(chat_id="default"):
    return SessionSource(platform="cli", user_id="local", chat_id=chat_id)


@pytest.mark.asyncio
async def test_session_rename_updates_index_and_database(store):
    session_store, db, _chain = store
    entry = await session_store.get_or_create("cli:old:local", _source("old"))

    ok = await session_store.rename_session("cli:old:local", "cli:new:local")

    assert ok
    assert session_store.get("cli:old:local") is None
    assert session_store.get("cli:new:local").session_id == entry.session_id
    assert await db.get_session_key(entry.session_id) == "cli:new:local"


@pytest.mark.asyncio
async def test_delete_session_removes_compression_descendants(store):
    session_store, db, chain = store
    entry = await session_store.get_or_create("cli:default:local", _source())
    await db.save_message(entry.session_id, "user", "old")
    compressed_id = await session_store.create_compressed_session(
        "cli:default:local",
        _source(),
        [{"role": "user", "content": [{"type": "text", "text": "summary"}]}],
    )

    await session_store.delete_session("cli:default:local")

    assert session_store.get("cli:default:local") is None
    assert await db.get_message_count(entry.session_id) == 0
    assert await db.get_message_count(compressed_id) == 0
    assert chain.get_chain(entry.session_id) == [entry.session_id]


@pytest.mark.asyncio
async def test_expire_sessions_removes_compression_descendants(store):
    session_store, db, chain = store
    entry = await session_store.get_or_create("cli:old:local", _source("old"))
    entry.last_active_at = time.time() - 10 * 86400
    session_store._save_index()
    compressed_id = await session_store.create_compressed_session(
        "cli:old:local",
        _source("old"),
        [{"role": "user", "content": [{"type": "text", "text": "summary"}]}],
    )

    count = await session_store.expire_sessions(max_age_days=1)

    assert count == 1
    assert session_store.get("cli:old:local") is None
    assert await db.get_message_count(compressed_id) == 0
    assert chain.get_chain(entry.session_id) == [entry.session_id]


@pytest.mark.asyncio
async def test_list_user_sessions_uses_resolved_message_count(store):
    session_store, db, _chain = store
    entry = await session_store.get_or_create("cli:default:local", _source())
    await db.save_message(entry.session_id, "user", "old")
    await session_store.create_compressed_session(
        "cli:default:local",
        _source(),
        [{"role": "user", "content": [{"type": "text", "text": "summary"}]}],
    )

    sessions = await session_store.list_user_sessions("cli", "local")

    assert sessions[0]["session_key"] == "cli:default:local"
    assert sessions[0]["current_session_id"] != entry.session_id
    assert sessions[0]["message_count"] == 1
