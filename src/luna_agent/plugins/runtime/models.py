"""Stable identities used by plugin generations and capability routes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class PluginRuntimeState(str, Enum):
    DISCOVERED = "discovered"
    PREPARING = "preparing"
    READY = "ready"
    ACTIVE = "active"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAILED = "failed"


class CapabilityKind(str, Enum):
    TOOL = "tool"
    SKILL = "skill"
    HOOK = "hook"
    COMMAND = "command"
    WORKFLOW = "workflow"
    PLATFORM = "platform"
    MCP_SERVER = "mcp_server"
    MEMORY_PROVIDER = "memory_provider"


@dataclass(frozen=True)
class CapabilityBinding:
    """One manager-owned capability implementation."""

    binding_id: str
    capability_id: str
    public_name: str
    kind: CapabilityKind
    owner: str
    generation_id: str
    runtime_instance_id: str
    contract_hash: str
    manager_key: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        required = {
            "binding_id": self.binding_id,
            "capability_id": self.capability_id,
            "public_name": self.public_name,
            "owner": self.owner,
            "generation_id": self.generation_id,
            "runtime_instance_id": self.runtime_instance_id,
            "manager_key": self.manager_key,
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError(f"Capability binding field(s) must not be empty: {', '.join(missing)}")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class CapabilityRoute:
    """Immutable public-name to manager-binding mapping."""

    capability_id: str
    public_name: str
    kind: CapabilityKind
    owner: str
    generation_id: str
    runtime_instance_id: str
    binding_id: str
    manager_key: str
    contract_hash: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_binding(cls, binding: CapabilityBinding) -> "CapabilityRoute":
        return cls(
            capability_id=binding.capability_id,
            public_name=binding.public_name,
            kind=binding.kind,
            owner=binding.owner,
            generation_id=binding.generation_id,
            runtime_instance_id=binding.runtime_instance_id,
            binding_id=binding.binding_id,
            manager_key=binding.manager_key,
            contract_hash=binding.contract_hash,
            metadata=MappingProxyType(dict(binding.metadata)),
        )
