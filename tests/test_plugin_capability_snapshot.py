import asyncio

import pytest

from personal_agent.plugins.runtime import (
    CandidateCatalog,
    CapabilityBinding,
    CapabilityKind,
    CapabilitySnapshotBuilder,
    CapabilityStore,
)


def _binding(
    name: str,
    *,
    owner: str = "plugin/demo",
    generation: str = "plugin/demo@g1",
    runtime: str = "runtime-1",
    kind: CapabilityKind = CapabilityKind.TOOL,
    priority: int = 100,
) -> CapabilityBinding:
    return CapabilityBinding(
        binding_id=f"{owner}:{generation}:{runtime}:{kind.value}:{name}",
        capability_id=f"{owner}:{kind.value}:{name}",
        public_name=name,
        kind=kind,
        owner=owner,
        generation_id=generation,
        runtime_instance_id=runtime,
        contract_hash=f"contract:{name}",
        manager_key=name,
        metadata={"priority": priority},
    )


def test_snapshot_builder_rejects_non_hook_name_conflicts():
    catalog = CandidateCatalog([
        _binding("convert", owner="plugin/a"),
        _binding("convert", owner="plugin/b"),
    ])

    with pytest.raises(ValueError, match="Capability conflict"):
        CapabilitySnapshotBuilder().build(catalog, revision=1)


def test_snapshot_builder_orders_hooks_and_projects_stable_fingerprint():
    catalog = CandidateCatalog([
        _binding("pre_tool_use", owner="plugin/late", kind=CapabilityKind.HOOK, priority=200),
        _binding("pre_tool_use", owner="plugin/early", kind=CapabilityKind.HOOK, priority=10),
        _binding("read_file"),
    ])
    snapshot = CapabilitySnapshotBuilder().build(catalog, revision=4)

    routes = snapshot.view().select(CapabilityKind.HOOK, "pre_tool_use")
    tool_view = snapshot.view({CapabilityKind.TOOL})

    assert [route.owner for route in routes] == ["plugin/early", "plugin/late"]
    assert tool_view.resolve(CapabilityKind.TOOL, "read_file") is not None
    assert tool_view.fingerprint == snapshot.view({CapabilityKind.TOOL}).fingerprint


@pytest.mark.asyncio
async def test_published_snapshot_retires_only_after_lease_release():
    retired = []
    builder = CapabilitySnapshotBuilder()
    first = builder.build(CandidateCatalog([_binding("v1")]), revision=1)
    second = builder.build(CandidateCatalog([_binding("v2")]), revision=2)
    store = CapabilityStore(first, on_retire=lambda snapshot: retired.append(snapshot.revision))

    lease = await store.acquire()
    await store.publish(second)

    assert lease.snapshot.revision == 1
    assert store.current.revision == 2
    assert retired == []

    await lease.release()

    assert retired == [1]
    assert store.health_snapshot()["active_leases"] == 0


@pytest.mark.asyncio
async def test_acquire_and_publish_keep_each_lease_on_one_revision():
    builder = CapabilitySnapshotBuilder()
    store = CapabilityStore(builder.build(CandidateCatalog([_binding("v1")]), revision=1))
    acquired = asyncio.Event()
    release = asyncio.Event()

    async def reader():
        async with await store.acquire() as lease:
            acquired.set()
            await release.wait()
            return lease.snapshot.revision

    task = asyncio.create_task(reader())
    await acquired.wait()
    await store.publish(builder.build(CandidateCatalog([_binding("v2")]), revision=2))
    release.set()

    assert await task == 1
    async with await store.acquire() as next_lease:
        assert next_lease.snapshot.revision == 2
