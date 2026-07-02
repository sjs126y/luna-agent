"""Atomic JSON state helpers and integration points."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from personal_agent.cron.store import CronStore
from personal_agent.db.database import Database
from personal_agent.gateway.auth import AuthManager
from personal_agent.gateway.compression_chain import CompressionChain
from personal_agent.gateway.session_store import SessionStore
from personal_agent.models.messages import SessionSource
from personal_agent.persistence.json_store import read_json_object, write_json_atomic
from personal_agent.plugins.core.manager import PluginManager


def test_json_store_writes_and_recovers_corrupt_object(tmp_path):
    path = tmp_path / "state.json"

    write_json_atomic(path, {"enabled": ["a"]})
    assert read_json_object(path, {}) == {"enabled": ["a"]}

    path.write_text("{broken", encoding="utf-8")
    data = read_json_object(path, {"enabled": []})

    assert data == {"enabled": []}
    backups = list(tmp_path.glob("state.json.corrupt.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{broken"


def test_json_store_treats_non_object_as_corrupt_for_object_reader(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("[]", encoding="utf-8")

    data = read_json_object(path, {"ok": True})

    assert data == {"ok": True}
    assert list(tmp_path.glob("state.json.corrupt.*"))


@pytest.mark.asyncio
async def test_session_store_recovers_corrupt_index(tmp_path):
    db = Database(tmp_path / "state.db")
    await db.initialize()
    try:
        (tmp_path / "sessions.json").write_text("{broken", encoding="utf-8")
        store = SessionStore(db, tmp_path)

        await store.initialize()
        entry = await store.get_or_create("cli:default:local", _source())

        assert entry.session_key == "cli:default:local"
        assert list(tmp_path.glob("sessions.json.corrupt.*"))
    finally:
        await db.close()


def test_compression_chain_recovers_corrupt_state(tmp_path):
    path = tmp_path / "compression_chain.json"
    path.write_text("{broken", encoding="utf-8")
    chain = CompressionChain(path)

    chain.load()
    chain.link("old", "new")

    assert chain.resolve("old") == "new"
    assert list(tmp_path.glob("compression_chain.json.corrupt.*"))


def test_cron_store_recovers_corrupt_jobs(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text("{broken", encoding="utf-8")
    store = CronStore(path)

    assert store.load_all() == []
    assert list(tmp_path.glob("jobs.json.corrupt.*"))


def test_auth_manager_recovers_corrupt_state(tmp_path):
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    (auth_dir / "allowlist.json").write_text("{broken", encoding="utf-8")
    (auth_dir / "pending.json").write_text("{broken", encoding="utf-8")

    manager = AuthManager(SimpleNamespace(auth_enabled=True, auth_admins=[]), tmp_path)

    assert manager.check("user-1", "")[0] is False
    assert list(auth_dir.glob("allowlist.json.corrupt.*"))
    assert list(auth_dir.glob("pending.json.corrupt.*"))


def test_plugin_manager_recovers_corrupt_state(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{broken", encoding="utf-8")

    manager = PluginManager(
        SimpleNamespace(agent_data_dir=tmp_path, plugins_dirs=[]),
        state_path=path,
        include_builtin=False,
    )

    assert manager.list_plugins() == []
    assert list(tmp_path.glob("state.json.corrupt.*"))


def _source() -> SessionSource:
    return SessionSource(platform="cli", user_id="local", chat_id="default", user_name="CLI")
