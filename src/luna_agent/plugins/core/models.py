"""Plugin data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from luna_agent_plugin_sdk import CommandEntry, PluginManifest
from luna_agent.plugins.runtime.models import (
    ActiveRuntimeStatus,
    PluginRuntimeState,
    RuntimeBackend,
    WorkerRuntimeStatus,
)


class PluginStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    DISABLED = "DISABLED"
    DEFERRED = "DEFERRED"
    LOADING = "LOADING"
    LOADED = "LOADED"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


@dataclass
class HookRegistration:
    plugin_key: str
    name: str
    callback: Any
    priority: int = 100


@dataclass
class PluginDefinition:
    key: str
    manifest: PluginManifest
    status: PluginStatus = PluginStatus.DISCOVERED
    error: str | None = None
    error_traceback: str | None = None
    deferred: bool = False
    enabled: bool = False


@dataclass
class GenerationRegistrations:
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    workflows: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    middleware: list[str] = field(default_factory=list)
    memory_providers: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "tools": len(self.tools),
            "skills": len(self.skills),
            "workflows": len(self.workflows),
            "platforms": len(self.platforms),
            "mcp_servers": len(self.mcp_servers),
            "hooks": len(self.hooks),
            "commands": len(self.commands),
            "middleware": len(self.middleware),
            "memory_providers": len(self.memory_providers),
        }


@dataclass
class PluginGeneration:
    generation_id: str = ""
    runtime_instance_id: str = ""
    runtime_state: PluginRuntimeState = PluginRuntimeState.DISCOVERED
    runtime_backend: RuntimeBackend = RuntimeBackend.IN_PROCESS
    module: Any | None = None
    ctx: Any | None = None
    module_namespace: str = ""
    package_digest: str = ""
    environment_id: str = ""
    environment_path: Any | None = None
    sandbox_backend: str = ""
    worker: Any | None = None
    worker_capabilities: Any | None = None
    worker_status: WorkerRuntimeStatus = field(default_factory=WorkerRuntimeStatus)
    generation_scope: Any | None = None
    active_registration: Any | None = None
    active_execution: Any | None = None
    active_status: ActiveRuntimeStatus = field(default_factory=ActiveRuntimeStatus)
    data_revision_id: str = ""
    data_path: Any | None = None
    registration_transaction: Any | None = None
    registrations: GenerationRegistrations = field(default_factory=GenerationRegistrations)


@dataclass(frozen=True)
class PluginView:
    """Immutable management projection of one definition and current generation."""

    key: str
    status: PluginStatus
    enabled: bool
    error: str
    generation_id: str
    runtime_instance_id: str
    runtime_state: PluginRuntimeState
    runtime_backend: RuntimeBackend
    worker: Mapping[str, Any]
    active: Mapping[str, Any]
    registrations: Mapping[str, int]

    @classmethod
    def capture(cls, plugin: "LoadedPlugin") -> "PluginView":
        return cls(
            key=plugin.key,
            status=plugin.status,
            enabled=plugin.enabled,
            error=str(plugin.error or ""),
            generation_id=plugin.generation_id,
            runtime_instance_id=plugin.runtime_instance_id,
            runtime_state=plugin.runtime_state,
            runtime_backend=plugin.runtime_backend,
            worker=MappingProxyType(plugin.worker_status.safe_summary()),
            active=MappingProxyType(plugin.active_status.safe_summary()),
            registrations=MappingProxyType(plugin.registration_counts()),
        )


class LoadedPlugin:
    """Compatibility facade over definition and generation-owned state."""

    def __init__(
        self,
        *,
        key: str,
        manifest: PluginManifest,
        status: PluginStatus = PluginStatus.DISCOVERED,
        error: str | None = None,
        error_traceback: str | None = None,
        deferred: bool = False,
        enabled: bool = False,
        runtime_state: PluginRuntimeState = PluginRuntimeState.DISCOVERED,
    ) -> None:
        self.definition = PluginDefinition(
            key=key,
            manifest=manifest,
            status=status,
            error=error,
            error_traceback=error_traceback,
            deferred=deferred,
            enabled=enabled,
        )
        self.generation = PluginGeneration(runtime_state=runtime_state)

    def view(self) -> PluginView:
        return PluginView.capture(self)

    def registration_counts(self) -> dict[str, int]:
        return self.generation.registrations.counts()

    @property
    def key(self) -> str:
        return self.definition.key

    @property
    def manifest(self) -> PluginManifest:
        return self.definition.manifest

    @manifest.setter
    def manifest(self, value: PluginManifest) -> None:
        self.definition.manifest = value

    @property
    def status(self) -> PluginStatus:
        return self.definition.status

    @status.setter
    def status(self, value: PluginStatus) -> None:
        self.definition.status = value

    @property
    def error(self) -> str | None:
        return self.definition.error

    @error.setter
    def error(self, value: str | None) -> None:
        self.definition.error = value

    @property
    def error_traceback(self) -> str | None:
        return self.definition.error_traceback

    @error_traceback.setter
    def error_traceback(self, value: str | None) -> None:
        self.definition.error_traceback = value

    @property
    def deferred(self) -> bool:
        return self.definition.deferred

    @deferred.setter
    def deferred(self, value: bool) -> None:
        self.definition.deferred = bool(value)

    @property
    def enabled(self) -> bool:
        return self.definition.enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self.definition.enabled = bool(value)

    @property
    def runtime_state(self) -> PluginRuntimeState:
        return self.generation.runtime_state

    @runtime_state.setter
    def runtime_state(self, value: PluginRuntimeState) -> None:
        self.generation.runtime_state = value

    @property
    def runtime_backend(self) -> RuntimeBackend:
        return self.generation.runtime_backend

    @runtime_backend.setter
    def runtime_backend(self, value: RuntimeBackend | str) -> None:
        self.generation.runtime_backend = (
            value if isinstance(value, RuntimeBackend) else RuntimeBackend(str(value))
        )


def _generation_property(name: str):
    def get(plugin: LoadedPlugin):
        return getattr(plugin.generation, name)

    def set_(plugin: LoadedPlugin, value) -> None:
        setattr(plugin.generation, name, value)

    return property(get, set_)


def _worker_status_property(name: str):
    def get(plugin: LoadedPlugin):
        return getattr(plugin.generation.worker_status, name)

    def set_(plugin: LoadedPlugin, value) -> None:
        setattr(plugin.generation.worker_status, name, value)

    return property(get, set_)


def _active_status_property(name: str):
    def get(plugin: LoadedPlugin):
        return getattr(plugin.generation.active_status, name)

    def set_(plugin: LoadedPlugin, value) -> None:
        setattr(plugin.generation.active_status, name, value)

    return property(get, set_)


for _name in (
    "generation_id",
    "runtime_instance_id",
    "module",
    "ctx",
    "module_namespace",
    "package_digest",
    "environment_id",
    "environment_path",
    "sandbox_backend",
    "worker",
    "worker_capabilities",
    "generation_scope",
    "active_registration",
    "data_revision_id",
    "data_path",
    "registration_transaction",
):
    setattr(LoadedPlugin, _name, _generation_property(_name))

LoadedPlugin.active_runner = _generation_property("active_execution")
LoadedPlugin.worker_status = property(lambda plugin: plugin.generation.worker_status)
LoadedPlugin.active_status = property(lambda plugin: plugin.generation.active_status)

for _public, _field in {
    "worker_state": "state",
    "worker_restart_count": "restart_count",
    "worker_failure_times": "failure_times",
    "worker_circuit_open": "circuit_open",
    "worker_last_error": "last_error",
    "worker_last_exit_at": "last_exit_at",
    "worker_next_retry_at": "next_retry_at",
}.items():
    setattr(LoadedPlugin, _public, _worker_status_property(_field))

for _public, _field in {
    "active_enabled": "enabled",
    "active_error": "error",
    "active_restart_count": "restart_count",
    "active_failure_times": "failure_times",
    "active_circuit_open": "circuit_open",
}.items():
    setattr(LoadedPlugin, _public, _active_status_property(_field))

for _public, _field in {
    "tools_registered": "tools",
    "skills_registered": "skills",
    "workflows_registered": "workflows",
    "platforms_registered": "platforms",
    "mcp_servers_registered": "mcp_servers",
    "hooks_registered": "hooks",
    "commands_registered": "commands",
    "middleware_registered": "middleware",
    "memory_providers_registered": "memory_providers",
}.items():
    setattr(
        LoadedPlugin,
        _public,
        property(
            lambda plugin, field_name=_field: getattr(
                plugin.generation.registrations, field_name
            )
        ),
    )
