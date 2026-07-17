from __future__ import annotations

import pytest
import pytest_asyncio

from personal_agent.artifacts import ArtifactStatus, ArtifactStore, ArtifactStoreError
from personal_agent.db.database import Database


@pytest_asyncio.fixture
async def artifact_db(tmp_path):
    database = Database(tmp_path / "state.db")
    await database.initialize()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_artifact_store_persists_and_selects_scoped_content(tmp_path, artifact_db):
    store = ArtifactStore(tmp_path / "artifacts", artifact_db, retention_hours=1)
    await store.initialize()

    ref = await store.create(
        b"png-data",
        kind="image",
        filename="../homepage.png",
        mime_type="image/png",
        session_key="wechat:user",
        turn_id="turn-1",
        source_name="screenshot",
    )

    assert ref.filename == "homepage.png"
    assert ref.delivery_eligible is True
    assert (await store.resolve_path(ref)).read_bytes() == b"png-data"
    selected = await store.select(
        ref.artifact_id,
        session_key="wechat:user",
        turn_id="turn-1",
    )
    assert selected.status == ArtifactStatus.SELECTED.value
    assert "relative_path" not in selected.safe_summary()


@pytest.mark.asyncio
async def test_artifact_store_rejects_scope_and_size(tmp_path, artifact_db):
    store = ArtifactStore(tmp_path / "artifacts", artifact_db, max_file_bytes=4)
    await store.initialize()

    with pytest.raises(ArtifactStoreError, match="exceeds"):
        await store.create(
            b"12345",
            kind="file",
            filename="large.bin",
            mime_type="application/octet-stream",
            session_key="cli:user",
            turn_id="turn-1",
        )

    ref = await store.create(
        b"1234",
        kind="file",
        filename="ok.bin",
        mime_type="application/octet-stream",
        session_key="cli:user",
        turn_id="turn-1",
    )
    with pytest.raises(ArtifactStoreError, match="artifact_scope_mismatch"):
        await store.select(ref.artifact_id, session_key="cli:other", turn_id="turn-1")


@pytest.mark.asyncio
async def test_artifact_store_cleans_only_unselected_expired_artifacts(tmp_path, artifact_db):
    store = ArtifactStore(tmp_path / "artifacts", artifact_db, retention_hours=1)
    await store.initialize()
    ref = await store.create(
        b"report",
        kind="file",
        filename="report.txt",
        mime_type="text/plain",
        session_key="cli:user",
        turn_id="turn-1",
    )

    assert await store.cleanup_expired(now=ref.expires_at + 1) == 1
    assert await store.get(ref.artifact_id) is None
