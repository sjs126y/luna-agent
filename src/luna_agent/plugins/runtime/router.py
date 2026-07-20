"""Generation-aware capability payload and snapshot routing."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from luna_agent.plugins.runtime.catalog import CandidateCatalog
from luna_agent.plugins.runtime.mapper import CapabilityMapper
from luna_agent.plugins.runtime.models import CapabilityBinding
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
        if core_payloads:
            self.payloads.update(core_payloads)
        active[owner] = bindings
        self.active_bindings = active
        return self.publish_current()

    def publish_staged(self, owner: str, runtime_instance_id: str) -> CapabilitySnapshot:
        bindings = self.runtime_bindings.get(runtime_instance_id)
        if bindings is None:
            raise RuntimeError(f"Staged plugin bindings are unavailable: {owner}")
        self.active_bindings = {**self.active_bindings, owner: bindings}
        return self.publish_current()

    def publish_without_owner(self, owner: str) -> CapabilitySnapshot | None:
        if owner not in self.active_bindings:
            return None
        self.active_bindings = {
            key: bindings for key, bindings in self.active_bindings.items() if key != owner
        }
        return self.publish_current()

    def restore_owner(self, owner: str, runtime_instance_id: str) -> CapabilitySnapshot | None:
        bindings = self.runtime_bindings.get(runtime_instance_id)
        if bindings is None:
            return None
        self.active_bindings = {**self.active_bindings, owner: bindings}
        return self.publish_current()

    def replace_dynamic_source(
        self,
        source_key: str,
        bindings: Iterable[CapabilityBinding],
        payloads: dict[str, Any],
    ) -> CapabilitySnapshot | None:
        staged = tuple(bindings)
        if staged == self.dynamic_bindings.get(source_key, ()):
            return None
        if staged:
            self.dynamic_bindings[source_key] = staged
        else:
            self.dynamic_bindings.pop(source_key, None)
        self.payloads.update(payloads)
        return self.publish_current()

    def build_current(self) -> CapabilitySnapshot:
        catalog = CandidateCatalog([
            binding
            for owner_bindings in self.active_bindings.values()
            for binding in owner_bindings
        ] + [
            binding
            for dynamic_bindings in self.dynamic_bindings.values()
            for binding in dynamic_bindings
        ])
        return self.builder.build(catalog, revision=self.store.current.revision + 1)

    def publish_current(self) -> CapabilitySnapshot:
        snapshot = self.store.publish_nowait(self.build_current())
        if self._on_publish is not None:
            result = self._on_publish(snapshot)
            if inspect.isawaitable(result):
                raise RuntimeError("Capability publish callback must be synchronous")
        return snapshot

    async def _retire(self, snapshot: CapabilitySnapshot) -> None:
        retained_ids = self.store.retained_binding_ids()
        for binding_id in snapshot.binding_ids - retained_ids:
            self.payloads.pop(binding_id, None)
        if self._on_retire is not None:
            result = self._on_retire(snapshot)
            if inspect.isawaitable(result):
                await result
