"""Per-plugin registration context."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
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

    def resolve_path(self, relative_path: str | Path) -> Path:
        """Resolve a path without allowing it to escape the plugin package."""
        root = self.plugin.manifest.path
        if root is None:
            raise ValueError(f"Plugin root is unavailable: {self.plugin.key}")
        root = root.resolve()
        candidate = Path(relative_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"Plugin path escapes package root: {relative_path}")
        return candidate

    def register_skills(self, relative_path: str | Path = "skills") -> int:
        """Discover and register skill metadata from this plugin package."""
        from personal_agent.skills.registry import discover_skills

        skills_dir = self.resolve_path(relative_path)
        if not skills_dir.is_dir():
            raise ValueError(f"Plugin skills directory does not exist: {relative_path}")
        return discover_skills(
            skills_dir,
            registrar=self,
            recursive=True,
            plugin_key=self.plugin.key,
            allowed_root=self.plugin.manifest.path,
        )

    def register_tool(self, entry: ToolEntry) -> None:
        from personal_agent.tools.registry import tool_registry

        existing = tool_registry.get(entry.name)
        should_register = self.manager.ensure_registration_available(
            "tool", entry.name, existing, entry, self.plugin.key,
        )
        self._mark_owner(entry if should_register else existing)
        if should_register:
            tool_registry.register(entry)
        if entry.name not in self.plugin.tools_registered:
            self.plugin.tools_registered.append(entry.name)

    def register_skill(self, entry: SkillEntry) -> None:
        from personal_agent.skills.registry import skill_registry

        existing = skill_registry.get(entry.name)
        should_register = self.manager.ensure_registration_available(
            "skill", entry.name, existing, entry, self.plugin.key,
        )
        self._mark_owner(entry if should_register else existing)
        if should_register:
            skill_registry.register(entry)
        if entry.name not in self.plugin.skills_registered:
            self.plugin.skills_registered.append(entry.name)

    def register_workflow(self, defn: WorkflowDef) -> None:
        from personal_agent.workflow.registry import workflow_registry

        existing = workflow_registry.get(defn.name)
        should_register = self.manager.ensure_registration_available(
            "workflow", defn.name, existing, defn, self.plugin.key,
        )
        self._mark_owner(defn if should_register else existing)
        if should_register:
            workflow_registry.register(defn)
        if defn.name not in self.plugin.workflows_registered:
            self.plugin.workflows_registered.append(defn.name)

    def register_platform(self, entry: PlatformEntry) -> None:
        from personal_agent.platforms.core import platform_registry

        existing = platform_registry.get(entry.name)
        should_register = self.manager.ensure_registration_available(
            "platform", entry.name, existing, entry, self.plugin.key,
        )
        self._mark_owner(entry if should_register else existing)
        if should_register:
            platform_registry.register(entry)
        if entry.name not in self.plugin.platforms_registered:
            self.plugin.platforms_registered.append(entry.name)

    def register_mcp_server(self, config: MCPServerConfig | dict[str, Any]) -> None:
        normalized = self.manager.register_mcp_server(self.plugin.key, config)
        label = normalized.name
        if label not in self.plugin.mcp_servers_registered:
            self.plugin.mcp_servers_registered.append(label)

    def register_mcp(self, relative_path: str | Path = "mcp.yaml") -> int:
        """Register MCP server configurations from a plugin-owned YAML or JSON file."""
        import yaml

        path = self.resolve_path(relative_path)
        if not path.is_file():
            raise ValueError(f"Plugin MCP config does not exist: {relative_path}")
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
        elif path.suffix.lower() in {".yaml", ".yml"}:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        else:
            raise ValueError(f"Plugin MCP config must be YAML or JSON: {relative_path}")
        if not isinstance(data, dict):
            raise ValueError("Plugin MCP config must be an object")
        servers = data.get("servers", [])
        if not isinstance(servers, list):
            raise ValueError("Plugin MCP config field 'servers' must be a list")
        for server in servers:
            if not isinstance(server, dict):
                raise ValueError("Plugin MCP server entry must be an object")
            self.register_mcp_server(server)
        return len(servers)

    def register_hook(
        self,
        event,
        callback,
        priority: int = 100,
        *,
        name: str = "",
        matcher: str = "*",
        timeout: float | None = None,
    ) -> None:
        """Register a typed runtime hook or a temporary legacy callback."""
        from personal_agent.hooks import HookEvent

        try:
            hook_event = event if isinstance(event, HookEvent) else HookEvent(str(event))
        except ValueError:
            self.manager.register_hook(self.plugin.key, str(event), callback, priority)
            label = f"legacy:{event}:{priority}"
        else:
            registration = self.manager.register_event_hook(
                self.plugin.key,
                hook_event,
                callback,
                name=name,
                matcher=matcher,
                priority=priority,
                timeout_seconds=timeout,
            )
            label = f"{hook_event.value}:{registration.name}:{priority}"
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

    def _mark_owner(self, entry: Any | None) -> None:
        if entry is None:
            return
        try:
            setattr(entry, "_plugin_key", self.plugin.key)
        except (AttributeError, TypeError):
            pass
        if hasattr(entry, "plugin_key"):
            try:
                setattr(entry, "plugin_key", self.plugin.key)
            except (AttributeError, TypeError):
                pass
