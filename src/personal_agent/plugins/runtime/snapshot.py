"""Immutable capability snapshots, projections, and usage leases."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from personal_agent.plugins.runtime.catalog import CandidateCatalog
from personal_agent.plugins.runtime.models import CapabilityKind, CapabilityRoute

RetireCallback = Callable[["CapabilitySnapshot"], Awaitable[None] | None]


@dataclass(frozen=True)
class CapabilityView:
    snapshot_revision: int
    routes: Mapping[CapabilityKind, Mapping[str, tuple[CapabilityRoute, ...]]]
    fingerprint: str

    def resolve(self, kind: CapabilityKind, name: str) -> CapabilityRoute | None:
        values = self.routes.get(kind, {}).get(name, ())
        return values[0] if values else None

    def select(self, kind: CapabilityKind, name: str) -> tuple[CapabilityRoute, ...]:
        return self.routes.get(kind, {}).get(name, ())

    def project(self, kinds: Iterable[CapabilityKind]) -> "CapabilityView":
        selected = set(kinds)
        routes = MappingProxyType({
            kind: self.routes[kind]
            for kind in sorted(selected, key=lambda item: item.value)
            if kind in self.routes
        })
        return CapabilityView(
            self.snapshot_revision,
            routes,
            _fingerprint(_route_rows(routes)),
        )


@dataclass(frozen=True)
class CapabilitySnapshot:
    revision: int
    routes: Mapping[CapabilityKind, Mapping[str, tuple[CapabilityRoute, ...]]]
    binding_ids: frozenset[str]
    fingerprint: str

    @classmethod
    def empty(cls) -> "CapabilitySnapshot":
        return cls(0, MappingProxyType({}), frozenset(), _fingerprint(()))

    def view(self, kinds: Iterable[CapabilityKind] | None = None) -> CapabilityView:
        selected = set(kinds) if kinds is not None else set(self.routes)
        routes = MappingProxyType({
            kind: self.routes[kind]
            for kind in sorted(selected, key=lambda item: item.value)
            if kind in self.routes
        })
        route_rows = _route_rows(routes)
        return CapabilityView(self.revision, routes, _fingerprint(route_rows))


class CapabilitySnapshotBuilder:
    """Validate a candidate catalog and produce an immutable route table."""

    def build(self, catalog: CandidateCatalog, *, revision: int) -> CapabilitySnapshot:
        if revision < 0:
            raise ValueError("Capability snapshot revision must be non-negative")
        grouped: dict[CapabilityKind, dict[str, list[CapabilityRoute]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for binding in catalog.bindings():
            grouped[binding.kind][binding.public_name].append(CapabilityRoute.from_binding(binding))

        frozen: dict[CapabilityKind, Mapping[str, tuple[CapabilityRoute, ...]]] = {}
        for kind, by_name in grouped.items():
            mapped: dict[str, tuple[CapabilityRoute, ...]] = {}
            for name, routes in by_name.items():
                if kind is not CapabilityKind.HOOK and len(routes) > 1:
                    owners = ", ".join(sorted({route.owner for route in routes}))
                    raise ValueError(
                        f"Capability conflict for {kind.value} '{name}': {owners}"
                    )
                if kind is CapabilityKind.HOOK:
                    routes.sort(key=lambda item: (
                        int(item.metadata.get("priority", 100)),
                        int(item.metadata.get("order", 0)),
                        item.binding_id,
                    ))
                mapped[name] = tuple(routes)
            frozen[kind] = MappingProxyType(mapped)
        routes = MappingProxyType(frozen)
        rows = _route_rows(routes)
        return CapabilitySnapshot(
            revision=revision,
            routes=routes,
            binding_ids=catalog.binding_ids(),
            fingerprint=_fingerprint(rows),
        )


class CapabilityLease:
    def __init__(self, store: "CapabilityStore", snapshot: CapabilitySnapshot) -> None:
        self._store = store
        self.snapshot = snapshot
        self._released = False

    def view(self, kinds: Iterable[CapabilityKind] | None = None) -> CapabilityView:
        return self.snapshot.view(kinds)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._store.release(self.snapshot.revision)

    async def __aenter__(self) -> "CapabilityLease":
        return self

    async def __aexit__(self, *_args) -> None:
        await self.release()


class CapabilityStore:
    def __init__(
        self,
        initial: CapabilitySnapshot | None = None,
        *,
        on_retire: RetireCallback | None = None,
    ) -> None:
        self._current = initial or CapabilitySnapshot.empty()
        self._refcounts: dict[int, int] = {self._current.revision: 0}
        self._retired: dict[int, CapabilitySnapshot] = {}
        self._lock = asyncio.Lock()
        self._on_retire = on_retire

    @property
    def current(self) -> CapabilitySnapshot:
        return self._current

    def retained_binding_ids(self) -> frozenset[str]:
        return frozenset().union(
            self._current.binding_ids,
            *(snapshot.binding_ids for snapshot in self._retired.values()),
        )

    def retained_runtime_ids(self) -> frozenset[str]:
        snapshots = [self._current, *self._retired.values()]
        return frozenset(
            route.runtime_instance_id
            for snapshot in snapshots
            for by_name in snapshot.routes.values()
            for routes in by_name.values()
            for route in routes
        )

    async def acquire(self) -> CapabilityLease:
        async with self._lock:
            snapshot = self._current
            self._refcounts[snapshot.revision] = self._refcounts.get(snapshot.revision, 0) + 1
        return CapabilityLease(self, snapshot)

    async def publish(self, snapshot: CapabilitySnapshot) -> CapabilitySnapshot:
        async with self._lock:
            previous = self._current
            if snapshot.revision <= previous.revision:
                raise ValueError("Published capability snapshot revision must increase")
            self._current = snapshot
            self._refcounts.setdefault(snapshot.revision, 0)
            self._retired[previous.revision] = previous
            releasable = self._collect_releasable_locked()
        await self._retire(releasable)
        return snapshot

    def publish_nowait(self, snapshot: CapabilitySnapshot) -> CapabilitySnapshot:
        """Publish from synchronous startup or registration code on the event-loop thread."""
        previous = self._current
        if snapshot.revision <= previous.revision:
            raise ValueError("Published capability snapshot revision must increase")
        self._current = snapshot
        self._refcounts.setdefault(snapshot.revision, 0)
        self._retired[previous.revision] = previous
        releasable = self._collect_releasable_locked()
        if releasable and self._on_retire is not None:
            result = self._retire(releasable)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(result)
            else:
                loop.create_task(result, name="capability-snapshot-retire")
        return snapshot

    async def release(self, revision: int) -> None:
        async with self._lock:
            count = self._refcounts.get(revision)
            if count is None or count <= 0:
                raise RuntimeError(f"Capability lease revision is not active: {revision}")
            self._refcounts[revision] = count - 1
            releasable = self._collect_releasable_locked()
        await self._retire(releasable)

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "current_revision": self._current.revision,
            "current_fingerprint": self._current.fingerprint,
            "current_bindings": len(self._current.binding_ids),
            "active_leases": sum(self._refcounts.values()),
            "leases_by_revision": dict(sorted(self._refcounts.items())),
            "retired_revisions": sorted(self._retired),
        }

    def _collect_releasable_locked(self) -> list[CapabilitySnapshot]:
        result: list[CapabilitySnapshot] = []
        for revision, snapshot in list(self._retired.items()):
            if self._refcounts.get(revision, 0) != 0:
                continue
            result.append(snapshot)
            self._retired.pop(revision, None)
            self._refcounts.pop(revision, None)
        return result

    async def _retire(self, snapshots: list[CapabilitySnapshot]) -> None:
        if self._on_retire is None:
            return
        for snapshot in snapshots:
            result = self._on_retire(snapshot)
            if inspect.isawaitable(result):
                await result


def _route_rows(
    routes: Mapping[CapabilityKind, Mapping[str, tuple[CapabilityRoute, ...]]],
) -> tuple[tuple[str, ...], ...]:
    return tuple(
        (
            kind.value,
            name,
            route.capability_id,
            route.generation_id,
            route.runtime_instance_id,
            route.binding_id,
            route.contract_hash,
        )
        for kind in sorted(routes, key=lambda item: item.value)
        for name in sorted(routes[kind])
        for route in routes[kind][name]
    )


def _fingerprint(rows: Iterable[tuple[str, ...]]) -> str:
    payload = json.dumps(list(rows), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
