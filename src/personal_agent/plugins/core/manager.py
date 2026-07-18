"""Plugin discovery, loading, hooks, commands, and diagnostics."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import re
import shutil
import sys
import traceback
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from personal_agent.persistence.json_store import read_json_object, write_json_atomic
from personal_agent.commands.registry import CORE_COMMAND_NAMES
from personal_agent.mcp.server_registry import MCPServerRegistry
from personal_agent.plugins.core.context import PluginRuntimeContext
from personal_agent.plugins.core.models import (
    CommandEntry,
    HookRegistration,
    LoadedPlugin,
    PluginManifest,
    PluginStatus,
)
from personal_agent.hooks import HookEvent, HookManager, HookSource
from personal_agent.plugins.runtime import (
    CandidateCatalog,
    CapabilityKind,
    CapabilityMapper,
    CapabilitySnapshotBuilder,
    CapabilityStore,
    PluginRuntimeState,
    PluginRuntimeManager,
)
from personal_agent.plugins.runtime.identity import (
    generation_id,
    package_digest,
    runtime_instance_id,
)
from personal_agent.plugins.runtime.importer import (
    cleanup_generation_namespace,
    generation_module_namespace,
    import_generation_entrypoint,
)
from personal_agent.plugins.install import PluginInstaller, PluginInstallStore

logger = logging.getLogger(__name__)

CORE_SLASH_COMMANDS = set(CORE_COMMAND_NAMES)

_BUILTIN_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "builtin"


class PluginManager:
    def __init__(
        self,
        settings: Any | None = None,
        *,
        plugin_dirs: Iterable[Path] | None = None,
        state_path: Path | None = None,
        include_builtin: bool = True,
        hook_manager: HookManager | None = None,
    ) -> None:
        self.settings = settings
        self._plugins: dict[str, LoadedPlugin] = {}
        self._hooks: dict[str, list[HookRegistration]] = {}
        self._commands: dict[str, CommandEntry] = {}
        self.hook_manager = hook_manager or HookManager()
        self.mcp_server_registry = MCPServerRegistry()
        self._conversation_coordinator = None
        self._delivery_service = None
        self._capability_mapper = CapabilityMapper()
        self._capability_builder = CapabilitySnapshotBuilder()
        self._active_bindings: dict[str, tuple[Any, ...]] = {}
        self._dynamic_bindings: dict[str, tuple[Any, ...]] = {}
        self._binding_payloads: dict[str, Any] = {}
        self._runtime_records: dict[str, LoadedPlugin] = {}
        self._runtime_bindings: dict[str, tuple[Any, ...]] = {}
        self._active_runtime_by_plugin: dict[str, str] = {}
        self._mcp_manager = None
        install_root = Path(getattr(settings, "agent_data_dir", "data")) / "plugins"
        self.install_store = PluginInstallStore(install_root / "install-state.json")
        self.installer = PluginInstaller(install_root)
        self._pending_package_removals: dict[str, dict[str, Any]] = {}
        self._plugin_tasks: dict[str, set[asyncio.Task]] = {}
        self._capability_view: ContextVar[Any | None] = ContextVar(
            f"plugin-capability-view:{id(self)}",
            default=None,
        )
        self.capability_store = CapabilityStore(on_retire=self._retire_snapshot)
        self.runtime_manager = PluginRuntimeManager(self)

        configured_dirs = list(getattr(settings, "plugins_dirs", []) or [])
        requested_dirs = list(plugin_dirs) if plugin_dirs is not None else configured_dirs
        base_dirs = [_BUILTIN_PLUGIN_DIR] if include_builtin else []
        self._plugin_dirs = self._dedupe_dirs([*base_dirs, *[Path(p) for p in requested_dirs]])

        data_dir = Path(getattr(settings, "agent_data_dir", "data"))
        self._state_path = Path(state_path) if state_path else data_dir / "plugins" / "state.json"
        self._state = self._load_state()

    @property
    def commands(self) -> dict[str, CommandEntry]:
        return dict(self._commands)

    @property
    def hooks(self) -> dict[str, list[HookRegistration]]:
        return {name: list(items) for name, items in self._hooks.items()}

    def bind_application_ports(self, *, conversation_coordinator, delivery_service) -> None:
        self._conversation_coordinator = conversation_coordinator
        self._delivery_service = delivery_service

    def bind_mcp_manager(self, manager) -> None:
        self._mcp_manager = manager

    def plugin_conversation_port(self, plugin):
        if self._conversation_coordinator is None:
            raise RuntimeError("active plugin runtime is unavailable")
        from personal_agent.plugins.core.ports import PluginConversationPort

        return PluginConversationPort(
            plugin=plugin,
            coordinator=self._conversation_coordinator,
        )

    def plugin_notification_port(self, plugin):
        if self._conversation_coordinator is None or self._delivery_service is None:
            raise RuntimeError("active plugin runtime is unavailable")
        from personal_agent.plugins.core.ports import PluginNotificationPort

        return PluginNotificationPort(
            plugin=plugin,
            coordinator=self._conversation_coordinator,
            delivery_service=self._delivery_service,
        )

    def plugin_storage_port(self, plugin):
        from personal_agent.plugins.core.ports import PluginStoragePort

        return PluginStoragePort(plugin=plugin, root=self.installer.data_root)

    def plugin_task_port(self, plugin):
        from personal_agent.plugins.core.ports import PluginTaskPort

        return PluginTaskPort(plugin=plugin, tasks=self._plugin_tasks)

    def discover(self) -> list[LoadedPlugin]:
        self.installer.cleanup_staging()
        for directory in self._plugin_dirs:
            source = self._source_for_directory(Path(directory))
            self._discover_dir(Path(directory), source=source, recursive=True)
        for package_path in self.install_store.active_paths():
            self._discover_dir(
                package_path,
                source="installed",
                recursive=False,
                allow_managed=True,
            )

        for plugin in self._plugins.values():
            if plugin.status == PluginStatus.ERROR:
                continue
            installed_enabled = self.install_store.enabled_for(plugin.key)
            plugin.enabled = (
                installed_enabled
                if plugin.manifest.source == "installed" and installed_enabled is not None
                else self._resolve_enabled(plugin.manifest)
            )
            if not plugin.enabled and plugin.status != PluginStatus.ERROR:
                plugin.status = PluginStatus.DISABLED
            elif plugin.manifest.deferred and plugin.status == PluginStatus.DISCOVERED:
                plugin.status = PluginStatus.DEFERRED

        return self.list_plugins()

    def load_enabled(self, *, include_deferred: bool = False) -> None:
        if not self._plugins:
            self.discover()
        for plugin in list(self._plugins.values()):
            if not plugin.enabled:
                plugin.status = PluginStatus.DISABLED
                continue
            if plugin.manifest.deferred and not include_deferred:
                if plugin.status not in (PluginStatus.LOADED, PluginStatus.ERROR):
                    plugin.status = PluginStatus.DEFERRED
                continue
            self.load_plugin(plugin.key)

    def load_plugin(self, key: str) -> LoadedPlugin:
        if not self._plugins:
            self.discover()
        plugin = self._plugins[key]
        if plugin.status == PluginStatus.LOADED:
            return plugin
        if not plugin.enabled:
            plugin.status = PluginStatus.DISABLED
            return plugin

        missing_env = self._missing_env(plugin.manifest)
        if missing_env:
            plugin.status = PluginStatus.ERROR
            plugin.error = f"Missing required env: {', '.join(missing_env)}"
            plugin.error_traceback = None
            return plugin

        plugin.status = PluginStatus.LOADING
        plugin.runtime_state = PluginRuntimeState.PREPARING
        plugin.error = None
        plugin.error_traceback = None
        before = self._registration_snapshot()
        try:
            all_config = getattr(self.settings, "plugins_config", {}) or {}
            plugin_config = all_config.get(plugin.key, {}) if isinstance(all_config, dict) else {}
            plugin.package_digest = package_digest(plugin.manifest.path)
            plugin.generation_id = generation_id(
                plugin.key,
                plugin.package_digest,
                plugin_config if isinstance(plugin_config, dict) else {},
            )
            plugin.runtime_instance_id = runtime_instance_id(plugin.key)
            if plugin.manifest.source != "builtin":
                plugin.module_namespace = generation_module_namespace(
                    plugin.key,
                    plugin.runtime_instance_id,
                )
            plugin.ctx = PluginRuntimeContext(self, plugin)
            module, register_fn = self._import_entrypoint(
                plugin.manifest,
                namespace=plugin.module_namespace,
            )
            plugin.module = module
            if register_fn is not None:
                result = register_fn(plugin.ctx)
                if inspect.isawaitable(result):
                    raise RuntimeError("Async plugin register() is not supported during synchronous load")
            elif hasattr(module, "register"):
                result = module.register(plugin.ctx)
                if inspect.isawaitable(result):
                    raise RuntimeError("Async plugin register() is not supported during synchronous load")
            after = self._registration_snapshot()
            self._assert_no_registry_replacements(before, after, plugin.key)
            if plugin.manifest.record_import_delta:
                self._record_registry_delta(plugin, before["names"], after["names"])
            self._publish_plugin_capabilities(plugin, after)
            plugin.status = PluginStatus.LOADED
            plugin.runtime_state = PluginRuntimeState.ACTIVE
            self._runtime_records[plugin.runtime_instance_id] = plugin
            self._active_runtime_by_plugin[plugin.key] = plugin.runtime_instance_id
            counts = plugin.registration_counts()
            logger.info(
                "Plugin loaded: %s skills=%d mcp=%d hooks=%d commands=%d",
                plugin.key,
                counts["skills"],
                counts["mcp_servers"],
                counts["hooks"],
                counts["commands"],
            )
        except Exception as exc:
            self.hook_manager.unregister_owner(plugin.runtime_instance_id)
            self._restore_registration_snapshot(before)
            self._clear_plugin_registrations(plugin)
            cleanup_generation_namespace(plugin.module_namespace)
            plugin.module = None
            plugin.ctx = None
            plugin.status = PluginStatus.ERROR
            plugin.runtime_state = PluginRuntimeState.FAILED
            plugin.error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            plugin.error_traceback = traceback.format_exc()
            logger.exception("Plugin '%s' failed to load", key)
        return plugin

    def ensure_registration_available(
        self,
        kind: str,
        name: str,
        existing: Any | None,
        candidate: Any,
        plugin_key: str,
    ) -> bool:
        """Reject cross-plugin replacement while allowing legacy idempotent registration."""
        if existing is None:
            return True
        owner = self._registration_owner(kind, name) or str(
            getattr(existing, "_plugin_key", "") or getattr(existing, "plugin_key", "")
        )
        if owner and owner != plugin_key:
            raise ValueError(f"{kind.title()} '{name}' is already registered by plugin '{owner}'")
        if owner == plugin_key:
            return existing is not candidate
        if existing is candidate or (not owner and existing == candidate):
            return False
        label = owner or "core runtime"
        raise ValueError(f"{kind.title()} '{name}' is already registered by {label}")

    def unload_plugin(self, key: str) -> LoadedPlugin:
        plugin = self._plugins[key]
        plugin.runtime_state = PluginRuntimeState.DRAINING
        self._remove_plugin_commands(key)
        self._remove_plugin_hooks(key)
        from personal_agent.platforms.core import platform_registry
        from personal_agent.skills.registry import skill_registry
        from personal_agent.tools.registry import tool_registry
        from personal_agent.workflow.registry import workflow_registry
        from personal_agent.memory.provider_registry import memory_provider_registry

        for name in list(plugin.tools_registered):
            tool_registry.unregister(name)
        for name in list(plugin.skills_registered):
            skill_registry.unregister(name)
        for name in list(plugin.workflows_registered):
            workflow_registry.unregister(name)
        for name in list(plugin.platforms_registered):
            platform_registry.unregister(name)

        self.mcp_server_registry.unregister_plugin(key)
        memory_provider_registry.unregister_plugin(key)
        self._clear_plugin_registrations(plugin)
        plugin.module = None
        plugin.ctx = None
        plugin.error = None
        plugin.error_traceback = None
        plugin.status = PluginStatus.DISABLED if not plugin.enabled else PluginStatus.DISCOVERED
        self._publish_without_owner(plugin.key)
        self._active_runtime_by_plugin.pop(plugin.key, None)
        return plugin

    def reload_plugin(self, key: str) -> LoadedPlugin:
        """Load a fresh generation and atomically replace the active route set."""
        if not self._plugins:
            self.discover()
        previous = self._plugins[key]
        if previous.status != PluginStatus.LOADED:
            return self.load_plugin(key)
        return self._activate_manifest(previous.manifest, previous=previous, evict=True)

    def _activate_manifest(
        self,
        manifest: PluginManifest,
        *,
        previous: LoadedPlugin | None = None,
        evict: bool = False,
        force_enabled: bool | None = None,
    ) -> LoadedPlugin:
        if evict and manifest.source == "builtin":
            self._evict_entrypoint_module(manifest)
        candidate = LoadedPlugin(
            key=manifest.key,
            manifest=manifest,
            status=PluginStatus.DISCOVERED,
            deferred=manifest.deferred,
            enabled=(
                bool(force_enabled)
                if force_enabled is not None
                else previous.enabled
                if previous is not None
                else self._resolve_enabled(manifest)
            ),
        )
        self._plugins[manifest.key] = candidate
        loaded = self.load_plugin(manifest.key)
        if loaded.status != PluginStatus.LOADED:
            if previous is not None:
                self._plugins[manifest.key] = previous
                previous.runtime_state = PluginRuntimeState.ACTIVE
            return loaded
        if previous is not None:
            previous.runtime_state = PluginRuntimeState.DRAINING
        return loaded

    async def reload_plugin_runtime(self, key: str) -> LoadedPlugin:
        previous = self._plugins.get(key)
        previous_runtime_id = previous.runtime_instance_id if previous is not None else ""
        loaded = self.reload_plugin(key)
        if loaded.status != PluginStatus.LOADED or self._mcp_manager is None:
            return loaded
        try:
            await self._mcp_manager.reconcile(self._desired_mcp_configs())
        except Exception:
            if previous_runtime_id:
                self._rollback_to_runtime(key, previous_runtime_id)
            raise
        return loaded

    async def unload_plugin_runtime(self, key: str) -> LoadedPlugin:
        plugin = self.unload_plugin(key)
        if self._mcp_manager is not None:
            await self._mcp_manager.reconcile(self._desired_mcp_configs())
        return plugin

    async def enable_plugin_runtime(self, key: str) -> LoadedPlugin:
        plugin = self.enable_plugin(key)
        self.install_store.set_enabled(key, True)
        loaded = self.load_plugin(plugin.key)
        if self._mcp_manager is not None:
            await self._mcp_manager.reconcile(self._desired_mcp_configs())
        return loaded

    async def disable_plugin_runtime(self, key: str) -> LoadedPlugin:
        plugin = self._plugins[key]
        plugin.enabled = False
        self._state.setdefault("disabled", [])
        if key not in self._state["disabled"]:
            self._state["disabled"].append(key)
        self._state["enabled"] = [item for item in self._state.get("enabled", []) if item != key]
        self._save_state()
        self.install_store.set_enabled(key, False)
        return await self.unload_plugin_runtime(key)

    async def install_plugin_runtime(
        self,
        source: Path | str,
        *,
        enable: bool = True,
    ) -> LoadedPlugin:
        package = self.installer.prepare(source)
        previous = self._plugins.get(package.manifest.key)
        if not enable and previous is None:
            self._add_manifest(package.manifest)
            plugin = self._plugins[package.manifest.key]
            plugin.enabled = False
            plugin.status = PluginStatus.DISABLED
            self.install_store.record_install(
                plugin_key=plugin.key,
                digest=package.digest,
                path=package.path,
                version=package.manifest.version,
                source=package.source,
                enabled=False,
            )
            return plugin
        try:
            loaded = self._activate_manifest(
                package.manifest,
                previous=previous,
                evict=previous is not None,
                force_enabled=enable,
            )
            if loaded.status != PluginStatus.LOADED:
                raise RuntimeError(loaded.error or f"Plugin failed to load: {loaded.key}")
            if self._mcp_manager is not None:
                await self._mcp_manager.reconcile(self._desired_mcp_configs())
        except Exception:
            if previous is not None and previous.runtime_instance_id:
                self._rollback_to_runtime(previous.key, previous.runtime_instance_id)
            self.installer.discard(package)
            raise
        self.install_store.record_install(
            plugin_key=loaded.key,
            digest=package.digest,
            path=package.path,
            version=package.manifest.version,
            source=package.source,
            enabled=enable,
        )
        return loaded

    async def rollback_plugin_runtime(self, key: str, digest: str) -> LoadedPlugin:
        path = self.install_store.package_path(key, digest)
        manifest_path = self._resolve_plugin_manifest_path(path)
        manifest = PluginManifest.from_mapping(
            self._read_manifest_file(manifest_path),
            source="installed",
            path=path,
        )
        previous = self._plugins.get(key)
        loaded = self._activate_manifest(manifest, previous=previous, evict=True)
        if loaded.status != PluginStatus.LOADED:
            if previous is not None:
                self._rollback_to_runtime(key, previous.runtime_instance_id)
            raise RuntimeError(loaded.error or f"Plugin rollback failed: {key}")
        if self._mcp_manager is not None:
            try:
                await self._mcp_manager.reconcile(self._desired_mcp_configs())
            except Exception:
                if previous is not None:
                    self._rollback_to_runtime(key, previous.runtime_instance_id)
                raise
        self.install_store.activate(key, digest)
        return loaded

    async def uninstall_plugin_runtime(
        self,
        key: str,
        *,
        purge_data: bool = False,
    ) -> LoadedPlugin:
        record = self.install_store.packages().get(key)
        if not isinstance(record, dict):
            raise KeyError(f"Installed plugin not found: {key}")
        self.install_store.mark_pending_removal(key)
        self._pending_package_removals[key] = {
            "paths": [
                Path(item["path"])
                for item in record.get("versions", {}).values()
                if isinstance(item, dict) and item.get("path")
            ],
            "purge_data": bool(purge_data),
        }
        plugin = await self.unload_plugin_runtime(key)
        self._finalize_pending_removals()
        return plugin

    def capability_payload(self, binding_id: str) -> Any | None:
        return self._binding_payloads.get(binding_id)

    def capability_health(self) -> dict[str, Any]:
        data = self.capability_store.health_snapshot()
        runtime_counts: dict[str, int] = {}
        for plugin in self._runtime_records.values():
            state = plugin.runtime_state.value
            runtime_counts[state] = runtime_counts.get(state, 0) + 1
        data.update({
            "active_plugin_owners": sorted(
                owner for owner in self._active_bindings if owner != "core"
            ),
            "payload_count": len(self._binding_payloads),
            "runtime_counts": runtime_counts,
            "install_revision": self.install_store.revision,
            "installed_packages": len(self.install_store.packages()),
            "pending_removals": sorted(self._pending_package_removals),
        })
        return data

    def refresh_mcp_tools(
        self,
        server_name: str,
        runtime_instance_id: str,
        tool_names: set[str],
    ) -> None:
        from personal_agent.tools.registry import tool_registry

        owner = self.mcp_server_registry.owner_for(server_name) or "configured-mcp"
        active_runtime_id = self._active_runtime_by_plugin.get(owner, "")
        plugin = self._runtime_records.get(active_runtime_id)
        generation = (
            plugin.generation_id
            if plugin is not None
            else f"configured-mcp@{server_name}"
        )
        bindings = []
        payloads = {}
        for name in sorted(tool_names):
            entry = tool_registry.get(name)
            if entry is None:
                continue
            binding = self._capability_mapper.binding(
                kind=CapabilityKind.TOOL,
                public_name=name,
                owner=owner,
                generation_id=generation,
                runtime_instance_id=runtime_instance_id,
                manager_key=name,
                contract=_tool_contract(entry),
            )
            bindings.append(binding)
            payloads[binding.binding_id] = entry
        source_key = f"mcp:{server_name}"
        previous = self._dynamic_bindings.get(source_key, ())
        if tuple(bindings) == previous:
            return
        if bindings:
            self._dynamic_bindings[source_key] = tuple(bindings)
        else:
            self._dynamic_bindings.pop(source_key, None)
        self._binding_payloads.update(payloads)
        self._publish_current_bindings()

    @contextmanager
    def bind_capability_view(self, view):
        from personal_agent.skills.registry import skill_registry
        from personal_agent.workflow.registry import workflow_registry

        hook_ids = {
            route.manager_key
            for routes in view.routes.get(CapabilityKind.HOOK, {}).values()
            for route in routes
        }
        skills = {
            name: self.capability_payload(routes[0].binding_id)
            for name, routes in view.routes.get(CapabilityKind.SKILL, {}).items()
            if routes and self.capability_payload(routes[0].binding_id) is not None
        }
        workflows = {
            name: self.capability_payload(routes[0].binding_id)
            for name, routes in view.routes.get(CapabilityKind.WORKFLOW, {}).items()
            if routes and self.capability_payload(routes[0].binding_id) is not None
        }
        token = self._capability_view.set(view)
        try:
            with ExitStack() as stack:
                stack.enter_context(self.hook_manager.bind_routes(hook_ids))
                stack.enter_context(skill_registry.bind_entries(skills))
                stack.enter_context(workflow_registry.bind_entries(workflows))
                yield
        finally:
            self._capability_view.reset(token)

    def enable_plugin(self, key: str) -> LoadedPlugin:
        if not self._plugins:
            self.discover()
        plugin = self._plugins[key]
        self._state.setdefault("enabled", [])
        self._state.setdefault("disabled", [])
        if key not in self._state["enabled"]:
            self._state["enabled"].append(key)
        self._state["disabled"] = [item for item in self._state["disabled"] if item != key]
        self._save_state()
        plugin.enabled = True
        plugin.status = PluginStatus.DEFERRED if plugin.manifest.deferred else PluginStatus.DISCOVERED
        return plugin

    def disable_plugin(self, key: str) -> LoadedPlugin:
        if not self._plugins:
            self.discover()
        plugin = self._plugins[key]
        self._state.setdefault("enabled", [])
        self._state.setdefault("disabled", [])
        if key not in self._state["disabled"]:
            self._state["disabled"].append(key)
        self._state["enabled"] = [item for item in self._state["enabled"] if item != key]
        self._save_state()
        plugin.enabled = False
        return self.unload_plugin(key)

    def register_hook(self, plugin_key: str, name: str, callback, priority: int = 100) -> None:
        reg = HookRegistration(plugin_key=plugin_key, name=name, callback=callback, priority=priority)
        self._hooks.setdefault(name, []).append(reg)
        self._hooks[name].sort(key=lambda item: item.priority)

    def register_event_hook(
        self,
        plugin_key: str,
        event: HookEvent | str,
        callback,
        *,
        name: str = "",
        matcher: str = "*",
        priority: int = 100,
        timeout_seconds: float | None = None,
    ):
        plugin = self._plugins.get(plugin_key)
        owner = plugin.runtime_instance_id if plugin is not None else plugin_key
        return self.hook_manager.register(
            owner=owner,
            source=HookSource.PLUGIN,
            event=event,
            callback=callback,
            name=name,
            matcher=matcher,
            priority=priority,
            timeout_seconds=timeout_seconds,
            active=plugin is None,
            managed=plugin is not None,
        )

    async def invoke_hook(self, name: str, *args, **kwargs) -> Any:
        result = None
        for reg in self._hooks.get(name, []):
            try:
                value = reg.callback(*args, **kwargs)
                if inspect.isawaitable(value):
                    value = await value
                if value is not None:
                    result = value
            except Exception:
                logger.exception("Plugin hook failed: plugin=%s hook=%s", reg.plugin_key, name)
        if result is None and args:
            return args[0]
        return result

    def register_command(self, entry: CommandEntry) -> None:
        entry.name = entry.name.lstrip("/")
        if entry.scope not in {"slash", "cli", "both"}:
            raise ValueError(f"Invalid command scope: {entry.scope}")
        if entry.name in CORE_SLASH_COMMANDS and entry.scope in {"slash", "both"}:
            raise ValueError(f"Plugin command cannot override core command: /{entry.name}")
        existing = self._commands.get(entry.name)
        if existing and existing.plugin_key != entry.plugin_key:
            raise ValueError(f"Plugin command already registered: /{entry.name}")
        self._commands[entry.name] = entry

    def get_command(self, name: str, *, scope: str = "slash") -> CommandEntry | None:
        normalized = name.lstrip("/")
        entry = None
        view = self._capability_view.get()
        if view is not None:
            route = view.resolve(CapabilityKind.COMMAND, normalized)
            if route is not None:
                entry = self.capability_payload(route.binding_id)
        if entry is None:
            entry = self._commands.get(normalized)
        if entry is None:
            return None
        if entry.scope not in {scope, "both"}:
            return None
        return entry

    async def execute_command(self, name: str, **kwargs) -> str | None:
        entry = self.get_command(name, scope=kwargs.pop("scope", "slash"))
        if entry is None:
            return None
        value = entry.handler(**kwargs)
        if inspect.isawaitable(value):
            value = await value
        return None if value is None else str(value)

    def register_mcp_server(self, plugin_key: str, config: Any):
        plugin = self._plugins.get(plugin_key)
        runtime_id = plugin.runtime_instance_id if plugin is not None else ""
        return self.mcp_server_registry.register(
            plugin_key,
            config,
            runtime_instance_id=runtime_id,
        )

    def get_mcp_servers(self) -> list[Any]:
        return self.mcp_server_registry.configs()

    def list_plugins(self) -> list[LoadedPlugin]:
        return [self._plugins[key] for key in sorted(self._plugins)]

    def doctor_plugin(self, key: str, *, check_entrypoint: bool | None = None) -> dict[str, Any]:
        if not self._plugins:
            self.discover()
        plugin = self._plugins[key]
        missing_env = self._missing_env(plugin.manifest)
        manifest_error = (plugin.error or "") if plugin.manifest.entrypoint == "invalid" else ""
        entrypoint_checked = False
        if manifest_error:
            entrypoint_ok, entrypoint_error = False, ""
        elif check_entrypoint is False:
            entrypoint_ok, entrypoint_error = True, ""
        elif check_entrypoint is None and plugin.status == PluginStatus.DEFERRED:
            entrypoint_ok, entrypoint_error = True, ""
        else:
            entrypoint_checked = True
            entrypoint_ok, entrypoint_error = self._check_entrypoint(plugin.manifest)
        return {
            "key": plugin.key,
            "name": plugin.manifest.name,
            "version": plugin.manifest.version,
            "schema_version": plugin.manifest.schema_version,
            "description": plugin.manifest.description,
            "kind": plugin.manifest.kind,
            "entrypoint": plugin.manifest.entrypoint,
            "provides": plugin.manifest.provides,
            "tags": plugin.manifest.tags,
            "enabled_by_default": plugin.manifest.enabled_by_default,
            "enabled": plugin.enabled,
            "status": plugin.status.value,
            "runtime_state": plugin.runtime_state.value,
            "generation_id": plugin.generation_id,
            "runtime_instance_id": plugin.runtime_instance_id,
            "package_digest": plugin.package_digest,
            "deferred": plugin.deferred,
            "source": plugin.manifest.source,
            "declared_source": plugin.manifest.declared_source or plugin.manifest.source,
            "path": str(plugin.manifest.path) if plugin.manifest.path else "",
            "manifest_path": self._manifest_path(plugin),
            "source_boundary": self._source_boundary(plugin),
            "requires_env": plugin.manifest.requires_env,
            "missing_env": missing_env,
            "manifest_valid": not manifest_error,
            "manifest_error": manifest_error,
            "manifest_unknown_fields": list(plugin.manifest.unknown_fields),
            "manifest_warnings": self._manifest_warnings(plugin),
            "boundary_warnings": self._boundary_warnings(plugin),
            "entrypoint_checked": entrypoint_checked,
            "entrypoint_importable": entrypoint_ok,
            "entrypoint_error": entrypoint_error,
            "deferred_reason": self._deferred_reason(plugin),
            "error": plugin.error or "",
            "error_traceback": plugin.error_traceback or "",
            "registered": plugin.registration_counts(),
            "registered_items": self._registered_items(plugin),
            "diagnostic_hints": self._diagnostic_hints(plugin, missing_env, entrypoint_ok, entrypoint_error),
        }

    def validate_plugin_path(self, path: Path, *, load: bool = True) -> dict[str, Any]:
        manifest_path = self._resolve_plugin_manifest_path(Path(path))
        plugin_dir = manifest_path.parent
        if not self._plugins:
            self.discover()

        matches = [
            plugin
            for plugin in self._plugins.values()
            if plugin.manifest.path and self._same_path(plugin.manifest.path, plugin_dir)
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Expected exactly one plugin manifest at {plugin_dir}, found {len(matches)}"
            )

        plugin = matches[0]
        if plugin.manifest.entrypoint != "invalid":
            plugin.enabled = True
            if plugin.status == PluginStatus.DISABLED:
                plugin.status = PluginStatus.DEFERRED if plugin.deferred else PluginStatus.DISCOVERED
            if load:
                plugin = self.load_plugin(plugin.key)

        report = self.doctor_plugin(plugin.key, check_entrypoint=True)
        report["validation_path"] = str(plugin_dir)
        report["validation_manifest"] = str(manifest_path)
        report["validation_load_requested"] = load
        report["validation_loaded"] = report["status"] == PluginStatus.LOADED.value
        report["validation_ok"] = (
            report["manifest_valid"]
            and report["entrypoint_importable"]
            and not report["missing_env"]
            and report["status"] != PluginStatus.ERROR.value
        )
        return report

    def _add_manifest(self, manifest: PluginManifest) -> None:
        if manifest.key in self._plugins:
            existing = self._plugins[manifest.key]
            if (
                existing.manifest.path is not None
                and manifest.path is not None
                and self._same_path(existing.manifest.path, manifest.path)
            ):
                return
            existing.status = PluginStatus.ERROR
            existing.error = f"Duplicate plugin key: {manifest.key}"
            existing.error_traceback = None
            return
        boundary_error = self._manifest_boundary_error(manifest)
        enabled = self._resolve_enabled(manifest)
        status = PluginStatus.ERROR if boundary_error else PluginStatus.DISABLED
        if enabled and not boundary_error:
            status = PluginStatus.DEFERRED if manifest.deferred else PluginStatus.DISCOVERED
        self._plugins[manifest.key] = LoadedPlugin(
            key=manifest.key,
            manifest=manifest,
            status=status,
            deferred=manifest.deferred,
            enabled=enabled and not boundary_error,
            error=boundary_error or None,
        )

    def _discover_dir(
        self,
        directory: Path,
        *,
        source: str | None = None,
        recursive: bool = False,
        allow_managed: bool = False,
    ) -> None:
        if not directory.exists():
            return
        manifest_files: list[Path] = []
        if recursive:
            manifest_files = sorted(
                path
                for path in directory.rglob("*")
                if path.is_file() and path.name in {"plugin.yaml", "plugin.yml", "plugin.json"}
                and (allow_managed or not self._is_managed_internal_path(path))
            )
        else:
            for child in sorted(directory.iterdir()):
                if child.is_dir():
                    for name in ("plugin.yaml", "plugin.yml", "plugin.json"):
                        candidate = child / name
                        if candidate.exists():
                            manifest_files.append(candidate)
                            break
                elif child.name in {"plugin.yaml", "plugin.yml", "plugin.json"}:
                    manifest_files.append(child)

        for manifest_path in manifest_files:
            try:
                data = self._read_manifest_file(manifest_path)
                manifest = PluginManifest.from_mapping(
                    data,
                    source=source or "local",
                    path=manifest_path.parent,
                )
                self._add_manifest(manifest)
            except Exception as exc:
                key = self._invalid_manifest_key(manifest_path)
                manifest = PluginManifest(
                    key=key,
                    name=manifest_path.parent.name,
                    version="0",
                    entrypoint="invalid",
                    source=source or "local",
                    path=manifest_path.parent,
                )
                self._plugins[key] = LoadedPlugin(
                    key=key,
                    manifest=manifest,
                    status=PluginStatus.ERROR,
                    error=str(exc),
                    enabled=False,
                )

    def _is_managed_internal_path(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path.absolute()
        for root in (
            self.installer.packages_root,
            self.installer.staging_root,
            self.installer.data_root,
        ):
            try:
                managed = root.resolve()
            except OSError:
                managed = root.absolute()
            if resolved == managed or managed in resolved.parents:
                return True
        return False

    def _read_manifest_file(self, path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            data = json.loads(text)
        else:
            data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError("Plugin manifest must be an object")
        return data

    def _resolve_plugin_manifest_path(self, path: Path) -> Path:
        target = path.expanduser()
        if not target.exists():
            raise ValueError(f"Plugin path does not exist: {target}")
        if target.is_file():
            if target.name not in {"plugin.yaml", "plugin.yml", "plugin.json"}:
                raise ValueError(f"Plugin manifest file must be plugin.yaml, plugin.yml, or plugin.json: {target}")
            return target

        direct = [
            target / name
            for name in ("plugin.yaml", "plugin.yml", "plugin.json")
            if (target / name).is_file()
        ]
        if len(direct) == 1:
            return direct[0]
        if len(direct) > 1:
            raise ValueError(f"Plugin directory has multiple manifest files: {target}")

        nested = sorted(
            item
            for item in target.rglob("*")
            if item.is_file() and item.name in {"plugin.yaml", "plugin.yml", "plugin.json"}
        )
        if not nested:
            raise ValueError(f"Plugin manifest not found under: {target}")
        if len(nested) > 1:
            choices = ", ".join(str(item) for item in nested[:5])
            raise ValueError(f"Plugin path contains multiple manifests; specify one plugin directory: {choices}")
        return nested[0]

    def _invalid_manifest_key(self, manifest_path: Path) -> str:
        raw_base = (manifest_path.parent.name or "manifest").lower()
        base = re.sub(r"[^a-z0-9_.-]+", "-", raw_base).strip("-._") or "manifest"
        key = f"invalid/{base}"
        if key not in self._plugins:
            return key
        index = 2
        while f"{key}-{index}" in self._plugins:
            index += 1
        return f"{key}-{index}"

    def _deferred_reason(self, plugin: LoadedPlugin) -> str:
        if not plugin.deferred:
            return ""
        if plugin.manifest.kind == "platform":
            return "平台插件会在网关解析平台适配器时加载"
        return ""

    def _diagnostic_hints(
        self,
        plugin: LoadedPlugin,
        missing_env: list[str],
        entrypoint_ok: bool,
        entrypoint_error: str,
    ) -> list[str]:
        hints: list[str] = []
        if plugin.manifest.entrypoint == "invalid":
            hints.append(f"修复插件 manifest: {plugin.error or 'invalid manifest'}")
            return hints
        if missing_env:
            hints.append(f"设置缺失环境变量: {', '.join(missing_env)}")
        if not entrypoint_ok:
            hints.append(f"修复入口导入: {entrypoint_error}")
        if plugin.status == PluginStatus.ERROR and plugin.error:
            hints.append(f"修复插件加载错误: {plugin.error}")
        if plugin.status == PluginStatus.DEFERRED:
            hints.append(self._deferred_reason(plugin))
        if not plugin.enabled:
            hints.append("插件已被配置或状态禁用")
        hints.extend(self._manifest_warnings(plugin))
        hints.extend(self._boundary_warnings(plugin))
        return [hint for hint in hints if hint]

    def _manifest_warnings(self, plugin: LoadedPlugin) -> list[str]:
        manifest = plugin.manifest
        if manifest.entrypoint == "invalid":
            return []
        warnings: list[str] = []
        if manifest.unknown_fields:
            warnings.append(f"Manifest 包含未知字段: {', '.join(manifest.unknown_fields)}")
        provides = set(manifest.provides)
        if manifest.kind == "platform" and not provides.intersection({"platform", "platforms"}):
            warnings.append("kind 为 platform 时建议 provides 包含 platform。")
        if manifest.kind == "mcp" and "mcp" not in provides:
            warnings.append("kind 为 mcp 时建议 provides 包含 mcp。")
        if manifest.kind == "platform" and not manifest.deferred:
            warnings.append("platform 插件建议设置 deferred: true，避免启动时 eager import。")
        bad_env = [
            name for name in manifest.requires_env
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name)
        ]
        if bad_env:
            warnings.append(f"requires_env 建议使用大写环境变量名: {', '.join(bad_env)}")
        return self._dedupe_strings(warnings)

    def _boundary_warnings(self, plugin: LoadedPlugin) -> list[str]:
        manifest = plugin.manifest
        if manifest.entrypoint == "invalid":
            return []
        warnings: list[str] = []
        boundary = self._source_boundary(plugin)
        declared = manifest.declared_source or manifest.source
        if declared != manifest.source:
            warnings.append(
                f"Manifest 声明 source={declared}，实际按扫描边界识别为 {manifest.source}。"
            )
        if boundary != "unknown" and manifest.source != boundary:
            warnings.append(
                f"Manifest source={manifest.source} 与路径边界 {boundary} 不一致。"
            )
        if manifest.source != "builtin" and manifest.kind == "builtin":
            warnings.append("用户插件不应声明 kind: builtin。")
        if manifest.source != "builtin" and manifest.key.startswith("builtin/"):
            warnings.append("用户插件不能使用 builtin/* 插件 key。")
        return self._dedupe_strings(warnings)

    def _manifest_boundary_error(self, manifest: PluginManifest) -> str:
        if manifest.source != "builtin" and manifest.key.startswith("builtin/"):
            return f"User plugin cannot use reserved builtin key: {manifest.key}"
        return ""

    @staticmethod
    def _dedupe_strings(items: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in items:
            if item in seen:
                continue
            result.append(item)
            seen.add(item)
        return result

    @staticmethod
    def _dedupe_dirs(directories: Iterable[Path]) -> list[Path]:
        seen: set[Path] = set()
        result: list[Path] = []
        for directory in directories:
            path = Path(directory).expanduser()
            try:
                key = path.resolve()
            except OSError:
                key = path.absolute()
            if key in seen:
                continue
            seen.add(key)
            result.append(path)
        return result

    def _resolve_enabled(self, manifest: PluginManifest) -> bool:
        enabled = set(getattr(self.settings, "plugins_enabled", []) or [])
        disabled = set(getattr(self.settings, "plugins_disabled", []) or [])
        enabled.update(self._state.get("enabled", []))
        disabled.update(self._state.get("disabled", []))
        if manifest.key in disabled:
            return False
        if manifest.key in enabled:
            return True
        return manifest.enabled_by_default

    def _load_state(self) -> dict[str, list[str]]:
        data = read_json_object(self._state_path, {"enabled": [], "disabled": []})
        return {
            "enabled": list(data.get("enabled", [])),
            "disabled": list(data.get("disabled", [])),
        }

    def _save_state(self) -> None:
        write_json_atomic(self._state_path, self._state)

    def _missing_env(self, manifest: PluginManifest) -> list[str]:
        missing = []
        for name in manifest.requires_env:
            resolver = getattr(self.settings, "get_env", None)
            value = resolver(name, "") if callable(resolver) else ""
            if value:
                continue
            setting_name = name.lower()
            if hasattr(self.settings, setting_name) and getattr(self.settings, setting_name):
                continue
            missing.append(name)
        return missing

    def _import_entrypoint(
        self,
        manifest: PluginManifest,
        *,
        namespace: str = "",
    ) -> tuple[ModuleType, Any | None]:
        entrypoint = manifest.entrypoint
        module_name, _, func_name = entrypoint.partition(":")
        if manifest.source != "builtin":
            if manifest.path is None:
                raise ValueError(f"Plugin path is unavailable: {manifest.key}")
            active_namespace = namespace or generation_module_namespace(
                manifest.key,
                runtime_instance_id(manifest.key),
            )
            return import_generation_entrypoint(
                plugin_root=manifest.path,
                entrypoint=entrypoint,
                namespace=active_namespace,
            )
        for path in self._import_paths_for_manifest(manifest):
            path_text = str(path)
            # Installed generations live in separate immutable directories. A rollback
            # must make the selected generation win over paths retained from newer ones.
            while path_text in sys.path:
                sys.path.remove(path_text)
            sys.path.insert(0, path_text)
        module = importlib.import_module(module_name)
        fn = getattr(module, func_name) if func_name else None
        return module, fn

    @staticmethod
    def _evict_entrypoint_module(manifest: PluginManifest) -> None:
        module_name = manifest.entrypoint.partition(":")[0]
        root_name = module_name.split(".", 1)[0]
        for loaded_name in list(sys.modules):
            if loaded_name == module_name or loaded_name.startswith(f"{module_name}."):
                sys.modules.pop(loaded_name, None)
            elif manifest.source != "builtin" and (
                loaded_name == root_name or loaded_name.startswith(f"{root_name}.")
            ):
                sys.modules.pop(loaded_name, None)
        if manifest.path is not None:
            for cached in manifest.path.rglob("__pycache__/*.pyc"):
                try:
                    cached.unlink()
                except OSError:
                    pass
        importlib.invalidate_caches()

    def _import_paths_for_manifest(self, manifest: PluginManifest) -> list[Path]:
        paths: list[Path] = []
        if manifest.path:
            paths.append(manifest.path)
            if (manifest.path / "__init__.py").is_file():
                paths.append(manifest.path.parent)
        return paths

    def _check_entrypoint(self, manifest: PluginManifest) -> tuple[bool, str]:
        namespace = ""
        try:
            if manifest.source != "builtin":
                namespace = generation_module_namespace(
                    manifest.key,
                    runtime_instance_id(f"validate/{manifest.key}"),
                )
            module, fn = self._import_entrypoint(manifest, namespace=namespace)
            if ":" in manifest.entrypoint and fn is None:
                return False, f"Entrypoint function not found: {manifest.entrypoint}"
            if fn is not None and not callable(fn):
                return False, f"Entrypoint is not callable: {manifest.entrypoint}"
            if fn is None and not hasattr(module, "register"):
                return False, "Module has no register() function"
            return True, ""
        except Exception as exc:
            return False, "".join(traceback.format_exception_only(type(exc), exc)).strip()
        finally:
            cleanup_generation_namespace(namespace)

    def _registered_items(self, plugin: LoadedPlugin) -> dict[str, list[str]]:
        return {
            "tools": list(plugin.tools_registered),
            "skills": list(plugin.skills_registered),
            "workflows": list(plugin.workflows_registered),
            "platforms": list(plugin.platforms_registered),
            "mcp_servers": list(plugin.mcp_servers_registered),
            "hooks": list(plugin.hooks_registered),
            "commands": list(plugin.commands_registered),
            "middleware": list(plugin.middleware_registered),
            "memory_providers": list(plugin.memory_providers_registered),
        }

    def _manifest_path(self, plugin: LoadedPlugin) -> str:
        if plugin.manifest.path is None:
            return ""
        for name in ("plugin.yaml", "plugin.yml", "plugin.json"):
            path = plugin.manifest.path / name
            if path.exists():
                return str(path)
        return ""

    def _source_boundary(self, plugin: LoadedPlugin) -> str:
        if plugin.manifest.path is None:
            return "unknown"
        if self._same_path(plugin.manifest.path, _BUILTIN_PLUGIN_DIR) or _BUILTIN_PLUGIN_DIR in plugin.manifest.path.parents:
            return "builtin"
        return self._source_for_directory(plugin.manifest.path)

    def _source_for_directory(self, directory: Path) -> str:
        if self._same_path(directory, _BUILTIN_PLUGIN_DIR) or _BUILTIN_PLUGIN_DIR in directory.parents:
            return "builtin"
        installed_root = Path(getattr(self.settings, "agent_data_dir", "data")) / "plugins"
        try:
            resolved = directory.resolve()
            installed = installed_root.resolve()
        except OSError:
            resolved = directory.absolute()
            installed = installed_root.absolute()
        if resolved == installed or installed in resolved.parents:
            return "installed"
        return "local"

    def _registry_snapshot(self) -> dict[str, set[str]]:
        from personal_agent.platforms.core import platform_registry
        from personal_agent.skills.registry import skill_registry
        from personal_agent.tools.registry import tool_registry
        from personal_agent.workflow.registry import workflow_registry

        return {
            "tools": tool_registry.all_names,
            "skills": {entry.name for entry in skill_registry.list()},
            "workflows": set(workflow_registry.list_names()),
            "platforms": {entry.name for entry in platform_registry.list()},
        }

    def _registration_snapshot(self) -> dict[str, Any]:
        from personal_agent.memory.provider_registry import memory_provider_registry
        from personal_agent.platforms.core import platform_registry
        from personal_agent.skills.registry import skill_registry
        from personal_agent.tools.registry import tool_registry
        from personal_agent.workflow.registry import workflow_registry

        entries = {
            "tools": {name: tool_registry.get(name) for name in tool_registry.all_names},
            "skills": {entry.name: entry for entry in skill_registry.list()},
            "workflows": {name: workflow_registry.get(name) for name in workflow_registry.list_names()},
            "platforms": {entry.name: entry for entry in platform_registry.list()},
            "memory_providers": {
                name: memory_provider_registry.get(name) for name in memory_provider_registry.names()
            },
        }
        return {
            "entries": entries,
            "names": {kind: set(values) for kind, values in entries.items() if kind != "memory_providers"},
            "commands": dict(self._commands),
            "hooks": {name: list(items) for name, items in self._hooks.items()},
            "mcp_servers": self.mcp_server_registry.snapshot(),
        }

    def _publish_plugin_capabilities(self, plugin: LoadedPlugin, snapshot: dict[str, Any]) -> None:
        bindings: list[Any] = []
        payloads: dict[str, Any] = {}

        def add(kind, name, payload, *, manager_key=None, contract=None, metadata=None, ordinal=0):
            binding = self._capability_mapper.binding(
                kind=kind,
                public_name=name,
                owner=plugin.key,
                generation_id=plugin.generation_id,
                runtime_instance_id=plugin.runtime_instance_id,
                manager_key=manager_key or name,
                contract=contract if contract is not None else payload,
                metadata=metadata,
                ordinal=ordinal,
            )
            bindings.append(binding)
            payloads[binding.binding_id] = payload

        entries = snapshot["entries"]
        for name in plugin.tools_registered:
            entry = entries["tools"].get(name)
            if entry is not None:
                add(CapabilityKind.TOOL, name, entry, contract=_tool_contract(entry))
        for name in plugin.skills_registered:
            entry = entries["skills"].get(name)
            if entry is not None:
                add(CapabilityKind.SKILL, name, entry, contract=_skill_contract(entry))
        for name in plugin.workflows_registered:
            entry = entries["workflows"].get(name)
            if entry is not None:
                add(CapabilityKind.WORKFLOW, name, entry, contract=_workflow_contract(entry))
        for name in plugin.platforms_registered:
            entry = entries["platforms"].get(name)
            if entry is not None:
                add(CapabilityKind.PLATFORM, name, entry, contract=_platform_contract(entry))
        for name in plugin.memory_providers_registered:
            entry = entries["memory_providers"].get(name)
            if entry is not None:
                add(CapabilityKind.MEMORY_PROVIDER, name, entry, contract=name)
        for name in plugin.commands_registered:
            entry = self._commands.get(name)
            if entry is not None:
                add(
                    CapabilityKind.COMMAND,
                    name,
                    entry,
                    contract={"description": entry.description, "scope": entry.scope},
                )

        typed_hooks = [
            item for item in self.hook_manager.registrations(include_inactive=True)
            if item.owner == plugin.runtime_instance_id
        ]
        for ordinal, registration in enumerate(typed_hooks):
            add(
                CapabilityKind.HOOK,
                registration.event.value,
                registration,
                manager_key=registration.hook_id,
                contract={
                    "event": registration.event.value,
                    "matcher": registration.matcher,
                    "name": registration.name,
                    "priority": registration.priority,
                },
                metadata={
                    "priority": registration.priority,
                    "order": registration.order,
                    "matcher": registration.matcher,
                },
                ordinal=ordinal,
            )
        mcp_entries, _revision = self.mcp_server_registry.snapshot()
        for name in plugin.mcp_servers_registered:
            registration = mcp_entries.get(name)
            if registration is not None:
                add(
                    CapabilityKind.MCP_SERVER,
                    name,
                    registration,
                    contract=registration.config,
                )

        core_bindings, core_payloads = self._capture_core_bindings(snapshot, plugin)
        active = {
            owner: values
            for owner, values in self._active_bindings.items()
            if owner not in {plugin.key, "core"}
        }
        active["core"] = tuple(core_bindings)
        active[plugin.key] = tuple(bindings)
        catalog = CandidateCatalog([
            binding
            for owner_bindings in active.values()
            for binding in owner_bindings
        ] + [
            binding
            for dynamic_bindings in self._dynamic_bindings.values()
            for binding in dynamic_bindings
        ])
        next_snapshot = self._capability_builder.build(
            catalog,
            revision=self.capability_store.current.revision + 1,
        )
        self._binding_payloads.update(core_payloads)
        self._binding_payloads.update(payloads)
        self._active_bindings = active
        self._runtime_bindings[plugin.runtime_instance_id] = tuple(bindings)
        self.capability_store.publish_nowait(next_snapshot)
        active_hook_ids = {
            route.manager_key
            for routes in next_snapshot.routes.get(CapabilityKind.HOOK, {}).values()
            for route in routes
        }
        self.hook_manager.activate_managed_routes(active_hook_ids)

    def _capture_core_bindings(
        self,
        snapshot: dict[str, Any],
        current_plugin: LoadedPlugin,
    ) -> tuple[list[Any], dict[str, Any]]:
        bindings: list[Any] = []
        payloads: dict[str, Any] = {}
        plugin_names = {
            "tools": set(current_plugin.tools_registered),
            "skills": set(current_plugin.skills_registered),
            "workflows": set(current_plugin.workflows_registered),
            "platforms": set(current_plugin.platforms_registered),
            "memory_providers": set(current_plugin.memory_providers_registered),
        }
        kind_map = {
            "tools": CapabilityKind.TOOL,
            "skills": CapabilityKind.SKILL,
            "workflows": CapabilityKind.WORKFLOW,
            "platforms": CapabilityKind.PLATFORM,
            "memory_providers": CapabilityKind.MEMORY_PROVIDER,
        }
        known_plugin_names: dict[str, set[str]] = {name: set() for name in kind_map}
        attributes = {
            "tools": "tools_registered",
            "skills": "skills_registered",
            "workflows": "workflows_registered",
            "platforms": "platforms_registered",
            "memory_providers": "memory_providers_registered",
        }
        for loaded in self._plugins.values():
            if loaded.key == current_plugin.key:
                continue
            for group, attribute in attributes.items():
                known_plugin_names[group].update(getattr(loaded, attribute))

        for group, entries in snapshot["entries"].items():
            if group not in kind_map:
                continue
            for name, payload in entries.items():
                if name in plugin_names[group] or name in known_plugin_names[group]:
                    continue
                if group == "tools" and name.startswith("mcp__"):
                    continue
                contract_fn = {
                    "tools": _tool_contract,
                    "skills": _skill_contract,
                    "workflows": _workflow_contract,
                    "platforms": _platform_contract,
                }.get(group, lambda item: name)
                binding = self._capability_mapper.binding(
                    kind=kind_map[group],
                    public_name=name,
                    owner="core",
                    generation_id="core@host-v1",
                    runtime_instance_id="host",
                    manager_key=name,
                    contract=contract_fn(payload),
                )
                bindings.append(binding)
                payloads[binding.binding_id] = payload
        return bindings, payloads

    def _publish_without_owner(self, owner: str) -> None:
        if owner not in self._active_bindings:
            return
        active = {
            key: bindings for key, bindings in self._active_bindings.items() if key != owner
        }
        catalog = CandidateCatalog([
            binding for owner_bindings in active.values() for binding in owner_bindings
        ] + [
            binding
            for dynamic_bindings in self._dynamic_bindings.values()
            for binding in dynamic_bindings
        ])
        snapshot = self._capability_builder.build(
            catalog,
            revision=self.capability_store.current.revision + 1,
        )
        self._active_bindings = active
        self.capability_store.publish_nowait(snapshot)
        active_hook_ids = {
            route.manager_key
            for routes in snapshot.routes.get(CapabilityKind.HOOK, {}).values()
            for route in routes
        }
        self.hook_manager.activate_managed_routes(active_hook_ids)

    def _publish_current_bindings(self) -> None:
        catalog = CandidateCatalog([
            binding
            for owner_bindings in self._active_bindings.values()
            for binding in owner_bindings
        ] + [
            binding
            for dynamic_bindings in self._dynamic_bindings.values()
            for binding in dynamic_bindings
        ])
        snapshot = self._capability_builder.build(
            catalog,
            revision=self.capability_store.current.revision + 1,
        )
        self.capability_store.publish_nowait(snapshot)
        active_hook_ids = {
            route.manager_key
            for routes in snapshot.routes.get(CapabilityKind.HOOK, {}).values()
            for route in routes
        }
        self.hook_manager.activate_managed_routes(active_hook_ids)

    def _retire_snapshot(self, snapshot) -> None:
        retained_ids = self.capability_store.retained_binding_ids()
        for binding_id in snapshot.binding_ids - retained_ids:
            self._binding_payloads.pop(binding_id, None)
        retained_runtimes = self.capability_store.retained_runtime_ids()
        runtime_ids = {
            route.runtime_instance_id
            for by_name in snapshot.routes.values()
            for routes in by_name.values()
            for route in routes
        }
        for runtime_id in runtime_ids - retained_runtimes - {"host"}:
            plugin = self._runtime_records.pop(runtime_id, None)
            if plugin is None or plugin.runtime_state is PluginRuntimeState.ACTIVE:
                continue
            self.hook_manager.unregister_owner(runtime_id)
            tasks = self._plugin_tasks.pop(runtime_id, set())
            for task in tasks:
                if not task.done():
                    task.cancel()
            cleanup_generation_namespace(plugin.module_namespace)
            plugin.ctx = None
            plugin.module = None
            plugin.runtime_state = PluginRuntimeState.STOPPED
            self._runtime_bindings.pop(runtime_id, None)
        if self._mcp_manager is not None:
            for runtime_id in runtime_ids - retained_runtimes:
                if not runtime_id.startswith("mcp:"):
                    continue
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(self._mcp_manager.retire_runtime(runtime_id))
                else:
                    loop.create_task(
                        self._mcp_manager.retire_runtime(runtime_id),
                        name=f"mcp-retire:{runtime_id}",
                    )
        self._finalize_pending_removals()

    def _finalize_pending_removals(self) -> None:
        retained_runtimes = self.capability_store.retained_runtime_ids()
        for plugin_key, pending in list(self._pending_package_removals.items()):
            runtime_ids = {
                runtime_id
                for runtime_id, plugin in self._runtime_records.items()
                if plugin.key == plugin_key
            }
            if runtime_ids & retained_runtimes:
                continue
            for path in pending.get("paths", []):
                target = Path(path)
                try:
                    resolved = target.resolve()
                    root = self.installer.packages_root.resolve()
                except OSError:
                    continue
                if root in resolved.parents:
                    shutil.rmtree(resolved, ignore_errors=True)
            if pending.get("purge_data"):
                data_path = self.installer.data_root / plugin_key.replace("/", "__")
                shutil.rmtree(data_path, ignore_errors=True)
            self.install_store.remove(plugin_key)
            current = self._plugins.get(plugin_key)
            if current is not None and current.runtime_state is not PluginRuntimeState.ACTIVE:
                self._plugins.pop(plugin_key, None)
            self._pending_package_removals.pop(plugin_key, None)

    def _desired_mcp_configs(self) -> list[Any]:
        return [
            *list(getattr(self.settings, "mcp_servers", []) or []),
            *self.get_mcp_servers(),
        ]

    def _rollback_to_runtime(self, plugin_key: str, runtime_id: str) -> None:
        previous = self._runtime_records.get(runtime_id)
        bindings = self._runtime_bindings.get(runtime_id)
        if previous is None or bindings is None:
            return
        current_id = self._active_runtime_by_plugin.get(plugin_key, "")
        current = self._runtime_records.get(current_id)
        if current is not None:
            current.runtime_state = PluginRuntimeState.DRAINING
        self._plugins[plugin_key] = previous
        previous.runtime_state = PluginRuntimeState.ACTIVE
        previous.status = PluginStatus.LOADED
        self._active_runtime_by_plugin[plugin_key] = runtime_id
        self._active_bindings[plugin_key] = bindings
        self._activate_binding_payloads(bindings)
        self._publish_current_bindings()

    def _activate_binding_payloads(self, bindings) -> None:
        from personal_agent.memory.provider_registry import memory_provider_registry
        from personal_agent.platforms.core import platform_registry
        from personal_agent.skills.registry import skill_registry
        from personal_agent.tools.registry import tool_registry
        from personal_agent.workflow.registry import workflow_registry

        for binding in bindings:
            payload = self.capability_payload(binding.binding_id)
            if payload is None:
                continue
            if binding.kind is CapabilityKind.TOOL:
                tool_registry.register(payload)
            elif binding.kind is CapabilityKind.SKILL:
                skill_registry.register(payload)
            elif binding.kind is CapabilityKind.WORKFLOW:
                workflow_registry.register(payload)
            elif binding.kind is CapabilityKind.PLATFORM:
                platform_registry.register(payload)
            elif binding.kind is CapabilityKind.COMMAND:
                self._commands[binding.public_name] = payload
            elif binding.kind is CapabilityKind.MCP_SERVER:
                self.mcp_server_registry.register(
                    payload.plugin_key,
                    payload.config,
                    runtime_instance_id=payload.runtime_instance_id,
                )
            elif binding.kind is CapabilityKind.MEMORY_PROVIDER:
                memory_provider_registry.register(
                    name=payload.name,
                    plugin_key=payload.plugin_key,
                    factory=payload.factory,
                    validator=payload.validator,
                )

    def _restore_registration_snapshot(self, snapshot: dict[str, Any]) -> None:
        from personal_agent.memory.provider_registry import memory_provider_registry
        from personal_agent.platforms.core import platform_registry
        from personal_agent.skills.registry import skill_registry
        from personal_agent.tools.registry import tool_registry
        from personal_agent.workflow.registry import workflow_registry

        entries = snapshot["entries"]
        self._restore_entry_map(
            entries["tools"],
            {name: tool_registry.get(name) for name in tool_registry.all_names},
            unregister=tool_registry.unregister,
            register=tool_registry.register,
        )
        self._restore_entry_map(
            entries["skills"],
            {entry.name: entry for entry in skill_registry.list()},
            unregister=skill_registry.unregister,
            register=skill_registry.register,
        )
        self._restore_entry_map(
            entries["workflows"],
            {name: workflow_registry.get(name) for name in workflow_registry.list_names()},
            unregister=workflow_registry.unregister,
            register=workflow_registry.register,
        )
        self._restore_entry_map(
            entries["platforms"],
            {entry.name: entry for entry in platform_registry.list()},
            unregister=platform_registry.unregister,
            register=platform_registry.register,
        )
        current_memory = {
            name: memory_provider_registry.get(name) for name in memory_provider_registry.names()
        }
        for name in set(current_memory) - set(entries["memory_providers"]):
            memory_provider_registry.unregister(name)
        for name, registration in entries["memory_providers"].items():
            if current_memory.get(name) is registration:
                continue
            memory_provider_registry.register(
                name=registration.name,
                plugin_key=registration.plugin_key,
                factory=registration.factory,
                validator=registration.validator,
            )

        self._commands = dict(snapshot["commands"])
        self._hooks = {name: list(items) for name, items in snapshot["hooks"].items()}
        self.mcp_server_registry.restore(snapshot["mcp_servers"])

    @staticmethod
    def _restore_entry_map(previous, current, *, unregister, register) -> None:
        for name in set(current) - set(previous):
            unregister(name)
        for name, entry in previous.items():
            if current.get(name) is not entry:
                register(entry)

    @staticmethod
    def _assert_no_registry_replacements(
        before: dict[str, Any],
        after: dict[str, Any],
        plugin_key: str,
    ) -> None:
        for kind, previous in before["entries"].items():
            current = after["entries"].get(kind, {})
            for name, entry in previous.items():
                if name in current and current[name] is not entry:
                    previous_owner = str(
                        getattr(entry, "_plugin_key", "") or getattr(entry, "plugin_key", "")
                    )
                    current_entry = current[name]
                    current_owner = str(
                        getattr(current_entry, "_plugin_key", "")
                        or getattr(current_entry, "plugin_key", "")
                    )
                    if previous_owner == plugin_key or current_owner == plugin_key:
                        continue
                    raise ValueError(f"Plugin replaced existing {kind.rstrip('s')} registration: {name}")

    def _record_registry_delta(
        self,
        plugin: LoadedPlugin,
        before: dict[str, set[str]],
        after: dict[str, set[str]],
    ) -> None:
        self._extend_unique(plugin.tools_registered, sorted(after["tools"] - before["tools"]))
        self._extend_unique(plugin.skills_registered, sorted(after["skills"] - before["skills"]))
        self._extend_unique(plugin.workflows_registered, sorted(after["workflows"] - before["workflows"]))
        self._extend_unique(plugin.platforms_registered, sorted(after["platforms"] - before["platforms"]))

    def _registration_owner(self, kind: str, name: str) -> str:
        attribute = {
            "tool": "tools_registered",
            "skill": "skills_registered",
            "workflow": "workflows_registered",
            "platform": "platforms_registered",
        }.get(kind)
        if attribute is None:
            return ""
        for plugin in self._plugins.values():
            if name in getattr(plugin, attribute):
                return plugin.key
        return ""

    @staticmethod
    def _clear_plugin_registrations(plugin: LoadedPlugin) -> None:
        plugin.tools_registered.clear()
        plugin.skills_registered.clear()
        plugin.workflows_registered.clear()
        plugin.platforms_registered.clear()
        plugin.mcp_servers_registered.clear()
        plugin.hooks_registered.clear()
        plugin.commands_registered.clear()
        plugin.middleware_registered.clear()
        plugin.memory_providers_registered.clear()
    def _extend_unique(self, target: list[str], values: list[str]) -> None:
        for value in values:
            if value not in target:
                target.append(value)

    def _remove_plugin_commands(self, plugin_key: str) -> None:
        for name, entry in list(self._commands.items()):
            if entry.plugin_key == plugin_key:
                del self._commands[name]

    def _remove_plugin_hooks(self, plugin_key: str) -> None:
        for name, regs in list(self._hooks.items()):
            self._hooks[name] = [reg for reg in regs if reg.plugin_key != plugin_key]
            if not self._hooks[name]:
                del self._hooks[name]

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        try:
            return left.resolve() == right.resolve()
        except OSError:
            return left.absolute() == right.absolute()


def _tool_contract(entry: Any) -> dict[str, Any]:
    return {
        "name": getattr(entry, "name", ""),
        "description": getattr(entry, "description", ""),
        "schema": getattr(entry, "schema", {}),
        "permission_category": getattr(entry, "permission_category", ""),
        "risk_level": getattr(entry, "risk_level", ""),
    }


def _skill_contract(entry: Any) -> dict[str, Any]:
    return {
        "name": getattr(entry, "name", ""),
        "description": getattr(entry, "description", ""),
        "path": getattr(entry, "path", ""),
        "triggers": list(getattr(entry, "triggers", []) or []),
    }


def _workflow_contract(entry: Any) -> dict[str, Any]:
    return {
        "name": getattr(entry, "name", ""),
        "description": getattr(entry, "description", ""),
        "phases": list(getattr(entry, "phases", []) or []),
        "when_to_use": getattr(entry, "when_to_use", ""),
    }


def _platform_contract(entry: Any) -> dict[str, Any]:
    capabilities = getattr(entry, "capabilities", None)
    return {
        "name": getattr(entry, "name", ""),
        "capabilities": repr(capabilities),
    }


def run_async(coro):
    return asyncio.run(coro)
