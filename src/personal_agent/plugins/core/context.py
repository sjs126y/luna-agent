"""Generation-bound runtime context exposed to plugins."""

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


class PluginRegistrationPort:
    """Group registration operations for one preparing plugin generation."""

    def __init__(self, context: "PluginRuntimeContext") -> None:
        self._context = context

    def tool(self, entry) -> None:
        self._context._register_tool(entry)

    def skill(self, entry) -> None:
        self._context._register_skill(entry)

    def skills(self, relative_path: str | Path = "skills") -> int:
        return self._context._register_skills(relative_path)

    def workflow(self, definition) -> None:
        self._context._register_workflow(definition)

    def platform(self, entry) -> None:
        self._context._register_platform(entry)

    def mcp_server(self, config) -> None:
        self._context._register_mcp_server(config)

    def mcp(self, relative_path: str | Path = "mcp.yaml") -> int:
        return self._context._register_mcp(relative_path)

    def hook(self, event, callback, priority: int = 100, **kwargs) -> None:
        self._context._register_hook(event, callback, priority, **kwargs)

    def command(self, entry) -> None:
        self._context._register_command(entry)

    def memory_provider(self, *, name: str, factory, validator) -> None:
        self._context._register_memory_provider(name=name, factory=factory, validator=validator)

    def active(
        self,
        *,
        run,
        resources=None,
        restart_policy="on_failure",
        startup_timeout: float = 20.0,
        shutdown_timeout: float = 20.0,
        on_quiesce=None,
        on_resume=None,
        on_stop=None,
    ) -> None:
        self._context._register_active(
            run=run,
            resources=resources,
            restart_policy=restart_policy,
            startup_timeout=startup_timeout,
            shutdown_timeout=shutdown_timeout,
            on_quiesce=on_quiesce,
            on_resume=on_resume,
            on_stop=on_stop,
        )


class PluginRuntimeContext:
    """Expose scoped registration and application ports to one runtime instance."""

    def __init__(self, manager: PluginManager, plugin: LoadedPlugin) -> None:
        self.manager = manager
        self.plugin = plugin
        self.settings = manager.settings
        all_config = getattr(manager.settings, "plugins_config", {}) or {}
        plugin_config = all_config.get(plugin.key, {}) if isinstance(all_config, dict) else {}
        if not isinstance(plugin_config, dict):
            raise ValueError(f"Plugin config must be an object: {plugin.key}")
        self._config = MappingProxyType(deepcopy(plugin_config))
        self.register = PluginRegistrationPort(self)
        self._resources = None

    @property
    def plugin_key(self) -> str:
        return self.plugin.key

    @property
    def generation_id(self) -> str:
        return self.plugin.generation_id

    @property
    def runtime_instance_id(self) -> str:
        return self.plugin.runtime_instance_id

    @property
    def state(self):
        return self.plugin.runtime_state

    @property
    def runtime(self):
        runner = self.plugin.active_runner
        if runner is None:
            raise RuntimeError(f"active plugin runtime is unavailable: {self.plugin.key}")
        return runner.control

    @property
    def resources(self):
        registration = self.plugin.active_registration
        if registration is None:
            raise RuntimeError(f"plugin has no active resource declaration: {self.plugin.key}")
        if self._resources is None:
            self._resources = self.manager.plugin_resource_facade(
                self.plugin,
                registration.resources,
            )
        return self._resources

    @property
    def config(self):
        """Return this plugin's isolated, read-only configuration."""
        return self._config

    @property
    def root(self):
        """Return the discovered plugin package root."""
        return self.plugin.manifest.path

    @property
    def conversation(self):
        """Return the capability-bound active conversation port."""
        return self.manager.plugin_conversation_port(self.plugin)

    @property
    def notifications(self):
        """Return the separately permissioned direct delivery port."""
        return self.manager.plugin_notification_port(self.plugin)

    @property
    def storage(self):
        return self.manager.plugin_storage_port(self.plugin)

    @property
    def tasks(self):
        return self.manager.plugin_task_port(self.plugin)

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

    def _require_registration(self) -> None:
        from personal_agent.plugins.runtime import PluginRuntimeState

        if self.plugin.runtime_state is not PluginRuntimeState.PREPARING:
            raise RuntimeError(
                f"plugin registration is closed: {self.plugin.key} "
                f"({self.plugin.runtime_state.value})"
            )

    def _register_skills(self, relative_path: str | Path = "skills") -> int:
        """Discover and register skill metadata from this plugin package."""
        self._require_registration()
        from personal_agent.skills.registry import discover_skills

        skills_dir = self.resolve_path(relative_path)
        if not skills_dir.is_dir():
            raise ValueError(f"Plugin skills directory does not exist: {relative_path}")
        return discover_skills(
            skills_dir,
            registrar=self.register,
            recursive=True,
            plugin_key=self.plugin.key,
            allowed_root=self.plugin.manifest.path,
        )

    def _register_tool(self, entry: ToolEntry) -> None:
        self._require_registration()
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

    def _register_skill(self, entry: SkillEntry) -> None:
        self._require_registration()
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

    def _register_workflow(self, defn: WorkflowDef) -> None:
        self._require_registration()
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

    def _register_platform(self, entry: PlatformEntry) -> None:
        self._require_registration()
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

    def _register_mcp_server(self, config: MCPServerConfig | dict[str, Any]) -> None:
        self._require_registration()
        normalized = self.manager.register_mcp_server(self.plugin.key, config)
        label = normalized.name
        if label not in self.plugin.mcp_servers_registered:
            self.plugin.mcp_servers_registered.append(label)

    def _register_mcp(self, relative_path: str | Path = "mcp.yaml") -> int:
        """Register MCP server configurations from a plugin-owned YAML or JSON file."""
        self._require_registration()
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
            self._register_mcp_server(server)
        return len(servers)

    def _register_hook(
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
        self._require_registration()
        from personal_agent.hooks import HookEvent

        retired = {
            "on_message_received",
            "on_before_send",
            "on_before_llm_call",
            "on_after_llm_call",
            "on_before_tool_exec",
            "on_after_tool_exec",
        }
        if str(event) in retired:
            raise ValueError(
                f"Legacy runtime hook '{event}' was removed; register a typed HookEvent instead"
            )
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

    def _register_command(self, entry: CommandEntry) -> None:
        self._require_registration()
        entry.plugin_key = self.plugin.key
        self.manager.register_command(entry)
        if entry.name not in self.plugin.commands_registered:
            self.plugin.commands_registered.append(entry.name)

    def _register_memory_provider(self, *, name: str, factory, validator) -> None:
        self._require_registration()
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

    def _register_active(
        self,
        *,
        run,
        resources=None,
        restart_policy="on_failure",
        startup_timeout: float = 20.0,
        shutdown_timeout: float = 20.0,
        on_quiesce=None,
        on_resume=None,
        on_stop=None,
    ) -> None:
        self._require_registration()
        if "active" not in set(self.plugin.manifest.provides or []):
            raise ValueError("Plugin must declare provides: [active] before registering a runner")
        if self.plugin.active_registration is not None:
            raise ValueError(f"Plugin can register only one active runner: {self.plugin.key}")
        from personal_agent.plugins.active import (
            ActiveRegistration,
            ActiveResourceRequest,
            ActiveRestartPolicy,
        )

        request = resources or ActiveResourceRequest()
        if not isinstance(request, ActiveResourceRequest):
            raise TypeError("Active plugin resources must be ActiveResourceRequest")
        self.plugin.active_registration = ActiveRegistration(
            run=run,
            resources=request,
            restart_policy=ActiveRestartPolicy(str(restart_policy)),
            startup_timeout=float(startup_timeout),
            shutdown_timeout=float(shutdown_timeout),
            on_quiesce=on_quiesce,
            on_resume=on_resume,
            on_stop=on_stop,
        )

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
