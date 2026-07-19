from __future__ import annotations

import pytest

from luna_agent.memory.internal import InternalMemoryConflict, InternalMemoryStore
from luna_agent.memory.models import InternalPatchAction, InternalPatchOperation


@pytest.mark.asyncio
async def test_internal_store_loads_profile_and_preserves_user_text(tmp_path) -> None:
    system = tmp_path / "system"
    profile = system / "work"
    profile.mkdir(parents=True)
    (profile / "USER.md").write_text("# User notes\n\nHand-written text.\n", encoding="utf-8")
    store = InternalMemoryStore(system, profile_map={"cli:work:u1": "work"})
    snapshot = store.snapshot(session_key="cli:work:u1")

    updated = await store.apply_operations(snapshot, [InternalPatchOperation(
        action=InternalPatchAction.ADD, observation_id="o1", target_file="USER.md",
        entry_id="preference-tea", content="Prefers green tea.",
    )])

    text = (profile / "USER.md").read_text(encoding="utf-8")
    assert "Hand-written text." in text
    assert "<!-- memory:preference-tea --> Prefers green tea." in text
    assert updated.revision != snapshot.revision


@pytest.mark.asyncio
async def test_internal_store_detects_revision_conflict(tmp_path) -> None:
    system = tmp_path / "system"
    system.mkdir()
    path = system / "MEMORY.md"
    path.write_text("original\n", encoding="utf-8")
    store = InternalMemoryStore(system)
    snapshot = store.snapshot()
    path.write_text("user changed it\n", encoding="utf-8")

    with pytest.raises(InternalMemoryConflict):
        await store.apply_operations(snapshot, [InternalPatchOperation(
            action=InternalPatchAction.ADD, observation_id="o1", target_file="MEMORY.md", content="new",
        )])


@pytest.mark.asyncio
async def test_internal_store_requires_confirmation_for_identity_files(tmp_path) -> None:
    system = tmp_path / "system"
    system.mkdir()
    (system / "SOUL.md").write_text("# Soul\n", encoding="utf-8")
    store = InternalMemoryStore(system)
    snapshot = store.snapshot()
    await store.apply_operations(snapshot, [InternalPatchOperation(
        action=InternalPatchAction.ADD, observation_id="o1", target_file="SOUL.md", content="Changed soul",
    )])

    assert "Changed soul" not in (system / "SOUL.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_internal_store_migrates_legacy_managed_marker(tmp_path) -> None:
    system = tmp_path / "system"
    system.mkdir()
    path = system / "USER.md"
    path.write_text(
        "Notes\n\n<!-- lumora-managed:start -->\n"
        "- <!-- memory:preference-tea --> Prefers tea.\n"
        "<!-- lumora-managed:end -->\n",
        encoding="utf-8",
    )
    store = InternalMemoryStore(system)
    snapshot = store.snapshot()

    await store.apply_operations(snapshot, [InternalPatchOperation(
        action=InternalPatchAction.UPDATE,
        observation_id="o2",
        target_file="USER.md",
        entry_id="preference-tea",
        content="Prefers green tea.",
    )])

    text = path.read_text(encoding="utf-8")
    assert "<!-- luna-managed:start -->" in text
    assert "<!-- lumora-managed:start -->" not in text
    assert "<!-- memory:preference-tea --> Prefers green tea." in text
