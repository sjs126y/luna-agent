"""Per-plugin registration context."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from personal_agent.adapters.base import PlatformEntry
    from personal_agent.mcp.client import MCPServerConfig
    from personal_agent.plugins.manager import PluginManager
    from personal_agent.plugins.models import CommandEntry, LoadedPlugin
    from personal_agent.skills.entry import SkillEntry
    from personal_agent.tools.entry import ToolEntry
    from personal_agent.workflow.registry import WorkflowDef


class PluginContext:
    """Forward registrations to subsystem registries and record plugin ownership."""

    def __init__(self, manager: PluginManager, plugin: LoadedPlugin) -> None:
        self.manager = manager
        self.plugin = plugin
        self.settings = manager.settings

    @property
    def plugin_key(self) -> str:
        return self.plugin.key

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
        from personal_agent.adapters.base import platform_registry

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
