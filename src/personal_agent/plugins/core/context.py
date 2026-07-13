"""Per-plugin registration context."""

from __future__ import annotations

from copy import deepcopy
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from personal_agent.platforms.core import PlatformEntry
    from personal_agent.mcp.models import MCPServerConfig
    from personal_agent.plugins.core.manager import PluginManager
    from personal_agent.plugins.core.models import CommandEntry, LoadedPlugin
    from personal_agent.skills.entry import SkillEntry
    from personal_agent.tools.entry import ToolEntry
    from personal_agent.workflow.registry import WorkflowDef


class PluginContext:
    """Forward registrations to subsystem registries and record plugin ownership."""

    def __init__(self, manager: PluginManager, plugin: LoadedPlugin) -> None:
        self.manager = manager
        self.plugin = plugin
        self.settings = manager.settings
        all_config = getattr(manager.settings, "plugins_config", {}) or {}
        plugin_config = all_config.get(plugin.key, {}) if isinstance(all_config, dict) else {}
        if not isinstance(plugin_config, dict):
            raise ValueError(f"Plugin config must be an object: {plugin.key}")
        self._config = MappingProxyType(deepcopy(plugin_config))

    @property
    def plugin_key(self) -> str:
        return self.plugin.key

    @property
    def config(self):
        """Return this plugin's isolated, read-only configuration."""
        return self._config

    @property
    def root(self):
        """Return the discovered plugin package root."""
        return self.plugin.manifest.path

    def parse_config(self, model_type):
        """Validate plugin configuration with a Pydantic model type."""
        validator = getattr(model_type, "model_validate", None)
        if not callable(validator):
            raise TypeError("Plugin config model must provide model_validate()")
        return validator(dict(self._config))

    def get_env(self, name: str, default: str = "") -> str:
        """Resolve an environment value through the Settings boundary."""
        resolver = getattr(self.settings, "get_env", None)
        return resolver(name, default) if callable(resolver) else default

    def register_tool(self, entry: ToolEntry) -> None:
        from personal_agent.tools.registry import tool_registry

        tool_registry.register(entry)
        if entry.name not in self.plugin.tools_registered:
            self.plugin.tools_registered.append(entry.name)

    def register_skill(self, entry: SkillEntry) -> None:
        from personal_agent.skills.registry import skill_registry

        skill_registry.register(entry)
        if entry.name not in self.plugin.skills_registered:
            self.plugin.skills_registered.append(entry.name)

    def register_workflow(self, defn: WorkflowDef) -> None:
        from personal_agent.workflow.registry import workflow_registry

        workflow_registry.register(defn)
        if defn.name not in self.plugin.workflows_registered:
            self.plugin.workflows_registered.append(defn.name)

    def register_platform(self, entry: PlatformEntry) -> None:
        from personal_agent.platforms.core import platform_registry

        platform_registry.register(entry)
        if entry.name not in self.plugin.platforms_registered:
            self.plugin.platforms_registered.append(entry.name)

    def register_mcp_server(self, config: MCPServerConfig | dict[str, Any]) -> None:
        name = getattr(config, "name", None)
        if name is None and isinstance(config, dict):
            name = str(config.get("name") or config.get("command") or "unknown")
        self.manager.register_mcp_server(self.plugin.key, config)
        label = str(name or "unknown")
        if label not in self.plugin.mcp_servers_registered:
            self.plugin.mcp_servers_registered.append(label)

    def register_hook(self, name: str, callback, priority: int = 100) -> None:
        self.manager.register_hook(self.plugin.key, name, callback, priority)
        label = f"{name}:{priority}"
        if label not in self.plugin.hooks_registered:
            self.plugin.hooks_registered.append(label)

    def register_command(self, entry: CommandEntry) -> None:
        entry.plugin_key = self.plugin.key
        self.manager.register_command(entry)
        if entry.name not in self.plugin.commands_registered:
            self.plugin.commands_registered.append(entry.name)

    def register_memory_provider(self, *, name: str, factory, validator) -> None:
        from personal_agent.memory.provider_registry import memory_provider_registry

        memory_provider_registry.register(
            name=name,
            plugin_key=self.plugin.key,
            factory=factory,
            validator=validator,
        )
        normalized = str(name).strip().lower()
        if normalized not in self.plugin.memory_providers_registered:
            self.plugin.memory_providers_registered.append(normalized)
