"""Mutable candidate catalog used before an atomic snapshot publication."""

from __future__ import annotations

from personal_agent.plugins.runtime.models import CapabilityBinding


class CandidateCatalog:
    def __init__(self, bindings: list[CapabilityBinding] | tuple[CapabilityBinding, ...] = ()) -> None:
        self._bindings: dict[str, CapabilityBinding] = {}
        for binding in bindings:
            self.add(binding)

    def add(self, binding: CapabilityBinding) -> CapabilityBinding:
        existing = self._bindings.get(binding.binding_id)
        if existing is not None and existing != binding:
            raise ValueError(f"Capability binding ID already exists: {binding.binding_id}")
        self._bindings[binding.binding_id] = binding
        return binding

    def extend(self, bindings) -> None:
        for binding in bindings:
            self.add(binding)

    def without_owner(self, owner: str) -> "CandidateCatalog":
        return CandidateCatalog([
            binding for binding in self._bindings.values() if binding.owner != owner
        ])

    def replace_owner(self, owner: str, bindings) -> "CandidateCatalog":
        result = self.without_owner(owner)
        result.extend(bindings)
        return result

    def bindings(self) -> tuple[CapabilityBinding, ...]:
        return tuple(sorted(self._bindings.values(), key=lambda item: item.binding_id))

    def binding_ids(self) -> frozenset[str]:
        return frozenset(self._bindings)
