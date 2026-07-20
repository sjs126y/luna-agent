"""Generation-aware plugin runtime primitives."""

from luna_agent.plugins.runtime.catalog import CandidateCatalog
from luna_agent.plugins.runtime.models import (
    ActiveRuntimeStatus,
    CapabilityBinding,
    CapabilityKind,
    CapabilityRoute,
    PluginRuntimeState,
    RuntimeBackend,
    WorkerRuntimeStatus,
)
from luna_agent.plugins.runtime.mapper import CapabilityMapper
from luna_agent.plugins.runtime.router import CapabilityRouter
from luna_agent.plugins.runtime.worker_supervisor import WorkerSupervisor
from luna_agent.plugins.runtime.snapshot import (
    CapabilityLease,
    CapabilitySnapshot,
    CapabilitySnapshotBuilder,
    CapabilityStore,
)

__all__ = [
    "CandidateCatalog",
    "ActiveRuntimeStatus",
    "CapabilityBinding",
    "CapabilityKind",
    "CapabilityLease",
    "CapabilityMapper",
    "CapabilityRouter",
    "CapabilityRoute",
    "CapabilitySnapshot",
    "CapabilitySnapshotBuilder",
    "CapabilityStore",
    "PluginRuntimeState",
    "RuntimeBackend",
    "WorkerRuntimeStatus",
    "WorkerSupervisor",
]
