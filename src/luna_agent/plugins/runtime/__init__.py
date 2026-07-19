"""Generation-aware plugin runtime primitives."""

from luna_agent.plugins.runtime.catalog import CandidateCatalog
from luna_agent.plugins.runtime.models import (
    CapabilityBinding,
    CapabilityKind,
    CapabilityRoute,
    PluginRuntimeState,
)
from luna_agent.plugins.runtime.mapper import CapabilityMapper
from luna_agent.plugins.runtime.snapshot import (
    CapabilityLease,
    CapabilitySnapshot,
    CapabilitySnapshotBuilder,
    CapabilityStore,
)

__all__ = [
    "CandidateCatalog",
    "CapabilityBinding",
    "CapabilityKind",
    "CapabilityLease",
    "CapabilityMapper",
    "CapabilityRoute",
    "CapabilitySnapshot",
    "CapabilitySnapshotBuilder",
    "CapabilityStore",
    "PluginRuntimeState",
]
