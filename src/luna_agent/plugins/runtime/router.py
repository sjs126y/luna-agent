"""Generation-aware capability payload and snapshot routing."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from luna_agent.plugins.runtime.catalog import CandidateCatalog
from luna_agent.plugins.runtime.mapper import CapabilityMapper
from luna_agent.plugins.runtime.models import CapabilityBinding, CapabilityKind
from luna_agent.plugins.runtime.snapshot import (
    CapabilitySnapshot,
    CapabilitySnapshotBuilder,
    CapabilityStore,
)


SnapshotCallback = Callable[[CapabilitySnapshot], Awaitable[None] | None]


class CapabilityRouter:
    """Own capability payloads, generation route sets, and atomic publication."""

    def __init__(
        self,
        *,
        on_publish: SnapshotCallback | None = None,
        on_retire: SnapshotCallback | None = None,
    ) -> None:
        self.mapper = CapabilityMapper()
        self.builder = CapabilitySnapshotBuilder()
        self.payloads: dict[str, Any] = {}
        self.runtime_bindings: dict[str, tuple[CapabilityBinding, ...]] = {}
        self.active_bindings: dict[str, tuple[CapabilityBinding, ...]] = {}
        self.dynamic_bindings: dict[str, tuple[CapabilityBinding, ...]] = {}
        self._on_publish = on_publish
        self._on_retire = on_retire
        self.store = CapabilityStore(on_retire=self._retire)

    def payload(self, binding_id: str) -> Any | None:
        return self.payloads.get(binding_id)

    def stage(
        self,
        runtime_instance_id: str,
        bindings: Iterable[CapabilityBinding],
        payloads: dict[str, Any],
    ) -> tuple[CapabilityBinding, ...]:
        staged = tuple(bindings)
        binding_ids = {binding.binding_id for binding in staged}
        unknown = set(payloads) - binding_ids
        if unknown:
            raise ValueError(
                "Capability payload has no matching binding: " + ", ".join(sorted(unknown))
            )
        self.payloads.update(payloads)
        self.runtime_bindings[runtime_instance_id] = staged
        return staged

    def discard_runtime(self, runtime_instance_id: str) -> tuple[CapabilityBinding, ...]:
        bindings = self.runtime_bindings.pop(runtime_instance_id, ())
        retained = self.store.retained_binding_ids()
        for binding in bindings:
            if binding.binding_id not in retained:
                self.payloads.pop(binding.binding_id, None)
        return bindings

    def publish_plugin(
        self,
        *,
        owner: str,
        runtime_instance_id: str,
        core_bindings: Iterable[CapabilityBinding] = (),
        core_payloads: dict[str, Any] | None = None,
        preserve_kinds: Iterable[CapabilityKind] = (),
    ) -> CapabilitySnapshot:
        bindings = self.runtime_bindings.get(runtime_instance_id)
        if bindings is None:
            raise RuntimeError(f"Staged plugin bindings are unavailable: {owner}")
        active = {
            key: values
            for key, values in self.active_bindings.items()
            if key not in {owner, "core"}
        }
        core = tuple(core_bindings)
        if core:
            active["core"] = core
        active[owner] = self._preserve_owner_kinds(
            owner,
            bindings,
            preserve_kinds,
        )
        payload_rollback = self._update_payloads(core_payloads or {})
        try:
            snapshot = self._publish_bindings(active, self.dynamic_bindings)
        except Exception:
            self._restore_payloads(payload_rollback)
            raise
        self.active_bindings = active
        return snapshot

    def publish_staged(
        self,
        owner: str,
        runtime_instance_id: str,
        *,
        preserve_kinds: Iterable[CapabilityKind] = (),
    ) -> CapabilitySnapshot:
        bindings = self.runtime_bindings.get(runtime_instance_id)
        if bindings is None:
            raise RuntimeError(f"Staged plugin bindings are unavailable: {owner}")
        active = {
            **self.active_bindings,
            owner: self._preserve_owner_kinds(owner, bindings, preserve_kinds),
        }
        snapshot = self._publish_bindings(active, self.dynamic_bindings)
        self.active_bindings = active
        return snapshot

    def publish_without_owner(
        self,
        owner: str,
        *,
        preserve_kinds: Iterable[CapabilityKind] = (),
    ) -> CapabilitySnapshot | None:
        if owner not in self.active_bindings:
            return None
        preserved = set(preserve_kinds)
        retained = tuple(
            binding
            for binding in self.active_bindings[owner]
            if binding.kind in preserved
        )
        active = {
            key: bindings for key, bindings in self.active_bindings.items() if key != owner
        }
        if retained:
            active[owner] = retained
        snapshot = self._publish_bindings(active, self.dynamic_bindings)
        self.active_bindings = active
        return snapshot

    def restore_owner(
        self,
        owner: str,
        runtime_instance_id: str,
        *,
        preserve_kinds: Iterable[CapabilityKind] = (),
    ) -> CapabilitySnapshot | None:
        bindings = self.runtime_bindings.get(runtime_instance_id)
        if bindings is None:
            return None
        active = {
            **self.active_bindings,
            owner: self._preserve_owner_kinds(owner, bindings, preserve_kinds),
        }
        snapshot = self._publish_bindings(active, self.dynamic_bindings)
        self.active_bindings = active
        return snapshot

    def replace_dynamic_source(
        self,
        source_key: str,
        bindings: Iterable[CapabilityBinding],
        payloads: dict[str, Any],
    ) -> CapabilitySnapshot | None:
        staged = tuple(bindings)
        if staged == self.dynamic_bindings.get(source_key, ()):
            return None
        dynamic = dict(self.dynamic_bindings)
        if staged:
            dynamic[source_key] = staged
        else:
            dynamic.pop(source_key, None)
        payload_rollback = self._update_payloads(payloads)
        try:
            snapshot = self._publish_bindings(self.active_bindings, dynamic)
        except Exception:
            self._restore_payloads(payload_rollback)
            raise
        self.dynamic_bindings = dynamic
        return snapshot

    def build_current(self) -> CapabilitySnapshot:
        return self._build(self.active_bindings, self.dynamic_bindings)

    def _build(
        self,
        active_bindings: dict[str, tuple[CapabilityBinding, ...]],
        dynamic_bindings: dict[str, tuple[CapabilityBinding, ...]],
    ) -> CapabilitySnapshot:
        catalog = CandidateCatalog([
            binding
            for owner_bindings in active_bindings.values()
            for binding in owner_bindings
        ] + [
            binding
            for source_bindings in dynamic_bindings.values()
            for binding in source_bindings
        ])
        return self.builder.build(catalog, revision=self.store.current.revision + 1)

    def publish_current(self) -> CapabilitySnapshot:
        return self._publish_bindings(self.active_bindings, self.dynamic_bindings)

    def _publish_bindings(
        self,
        active_bindings: dict[str, tuple[CapabilityBinding, ...]],
        dynamic_bindings: dict[str, tuple[CapabilityBinding, ...]],
    ) -> CapabilitySnapshot:
        snapshot = self._build(active_bindings, dynamic_bindings)
        previous = self.store.current
        try:
            if self._on_publish is not None:
                result = self._on_publish(snapshot)
                if inspect.isawaitable(result):
                    raise RuntimeError("Capability publish callback must be synchronous")
            return self.store.publish_nowait(snapshot)
        except Exception:
            # The route callback may have changed a derived registry before it
            # raised. Re-apply the last committed route set and leave the
            # CapabilityStore untouched when publication did not complete.
            if self._on_publish is not None:
                try:
                    result = self._on_publish(previous)
                    if inspect.isawaitable(result):
                        raise RuntimeError("Capability publish callback must be synchronous")
                except Exception:
                    pass
            raise

    def _preserve_owner_kinds(
        self,
        owner: str,
        staged: tuple[CapabilityBinding, ...],
        preserve_kinds: Iterable[CapabilityKind],
    ) -> tuple[CapabilityBinding, ...]:
        preserved = set(preserve_kinds)
        if not preserved:
            return staged
        live = tuple(
            binding
            for binding in self.active_bindings.get(owner, ())
            if binding.kind in preserved
        )
        replaceable = tuple(
            binding for binding in staged if binding.kind not in preserved
        )
        return (*replaceable, *live)

    def _update_payloads(self, payloads: dict[str, Any]) -> dict[str, tuple[bool, Any]]:
        previous = {
            binding_id: (binding_id in self.payloads, self.payloads.get(binding_id))
            for binding_id in payloads
        }
        self.payloads.update(payloads)
        return previous

    def _restore_payloads(self, previous: dict[str, tuple[bool, Any]]) -> None:
        for binding_id, (existed, payload) in previous.items():
            if existed:
                self.payloads[binding_id] = payload
            else:
                self.payloads.pop(binding_id, None)

    async def _retire(self, snapshot: CapabilitySnapshot) -> None:
        retained_ids = self.store.retained_binding_ids()
        for binding_id in snapshot.binding_ids - retained_ids:
            self.payloads.pop(binding_id, None)
        if self._on_retire is not None:
            result = self._on_retire(snapshot)
            if inspect.isawaitable(result):
                await result
