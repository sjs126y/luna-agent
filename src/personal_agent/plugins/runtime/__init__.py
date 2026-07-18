"""Generation-aware plugin runtime primitives."""

from personal_agent.plugins.runtime.catalog import CandidateCatalog
from personal_agent.plugins.runtime.models import (
    CapabilityBinding,
    CapabilityKind,
    CapabilityRoute,
    PluginRuntimeState,
)
from personal_agent.plugins.runtime.snapshot import (
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
    "CapabilityRoute",
    "CapabilitySnapshot",
    "CapabilitySnapshotBuilder",
    "CapabilityStore",
    "PluginRuntimeState",
]
