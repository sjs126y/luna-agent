"""Plugin data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from luna_agent_plugin_sdk import CommandEntry, PluginManifest
from luna_agent.plugins.runtime.models import PluginRuntimeState


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
class LoadedPlugin:
    key: str
    manifest: PluginManifest
    status: PluginStatus = PluginStatus.DISCOVERED
    module: Any | None = None
    ctx: Any | None = None
    error: str | None = None
    error_traceback: str | None = None
    deferred: bool = False
    enabled: bool = False
    generation_id: str = ""
    runtime_instance_id: str = ""
    module_namespace: str = ""
    package_digest: str = ""
    runtime_state: PluginRuntimeState = PluginRuntimeState.DISCOVERED
    generation_scope: Any | None = None
    active_registration: Any | None = None
    active_runner: Any | None = None
    active_enabled: bool = False
    active_error: str = ""
    active_restart_count: int = 0
    active_failure_times: list[float] = field(default_factory=list)
    active_circuit_open: bool = False
    data_revision_id: str = ""
    data_path: Any | None = None
    prepare_rollback_snapshot: Any | None = None
    tools_registered: list[str] = field(default_factory=list)
    skills_registered: list[str] = field(default_factory=list)
    workflows_registered: list[str] = field(default_factory=list)
    platforms_registered: list[str] = field(default_factory=list)
    mcp_servers_registered: list[str] = field(default_factory=list)
    hooks_registered: list[str] = field(default_factory=list)
    commands_registered: list[str] = field(default_factory=list)
    middleware_registered: list[str] = field(default_factory=list)
    memory_providers_registered: list[str] = field(default_factory=list)

    def registration_counts(self) -> dict[str, int]:
        return {
            "tools": len(self.tools_registered),
            "skills": len(self.skills_registered),
            "workflows": len(self.workflows_registered),
            "platforms": len(self.platforms_registered),
            "mcp_servers": len(self.mcp_servers_registered),
            "hooks": len(self.hooks_registered),
            "commands": len(self.commands_registered),
            "middleware": len(self.middleware_registered),
            "memory_providers": len(self.memory_providers_registered),
        }
