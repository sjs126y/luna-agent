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

from luna_agent.persistence.json_store import read_json_object, write_json_atomic
from luna_agent.commands.registry import CORE_COMMAND_NAMES
from luna_agent.mcp.server_registry import MCPServerRegistry
from luna_agent.plugins.core.context import PluginRuntimeContext
from luna_agent.plugins.core.coordinator import GenerationCoordinator
from luna_agent.plugins.core.models import (
    CommandEntry,
    HookRegistration,
    LoadedPlugin,
    PluginManifest,
    PluginStatus,
)
from luna_agent.hooks import HookEvent, HookManager, HookSource
from luna_agent.plugins.runtime import (
    CapabilityKind,
    CapabilityRouter,
    PluginRuntimeState,
    RuntimeBackend,
)
from luna_agent.plugins.runtime.identity import (
    generation_id,
    package_digest,
    runtime_instance_id,
)
from luna_agent.plugins.runtime.importer import (
    cleanup_generation_namespace,
    generation_module_namespace,
    import_generation_entrypoint,
)
from luna_agent.plugins.install import PluginInstaller, PluginInstallStore
from luna_agent.plugins.runtime.external_service import ExternalPluginRuntimeService
from luna_agent.plugins.active import (
    ActiveSupervisor,
    PluginDataRevisionStore,
    PluginGenerationScope,
)

logger = logging.getLogger(__name__)

CORE_SLASH_COMMANDS = set(CORE_COMMAND_NAMES)
BOOT_SCOPED_CAPABILITY_KINDS = frozenset({
    CapabilityKind.PLATFORM,
    CapabilityKind.MEMORY_PROVIDER,
})

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
        self._artifact_store = None
        self._event_port_factory = None
        self._runtime_records: dict[str, LoadedPlugin] = {}
        self._active_runtime_by_plugin: dict[str, str] = {}
        self._mcp_manager = None
        self._boot_scope_sealed = False
        self._pending_boot_scope: dict[str, set[CapabilityKind]] = {}
        install_root = Path(getattr(settings, "agent_data_dir", "data")) / "plugins"
        self.install_store = PluginInstallStore(install_root / "install-state.json")
        self.installer = PluginInstaller(install_root)
        self.install_store.repair_paths(self.installer.packages_root)
        self.external_runtime = ExternalPluginRuntimeService(self, install_root)
        self.data_revisions = PluginDataRevisionStore(self.installer.data_root)
        from luna_agent.plugins.control_state import PluginControlStateStore
        from luna_agent.plugins.events import PluginEventJournal
        from luna_agent.plugins.operations import PluginOperationTracker

        self._control_store = PluginControlStateStore(install_root / "control-state.json")
        self.events = PluginEventJournal(self._control_store)
        self.operations = PluginOperationTracker(self._control_store, self.events)
        self.active_supervisor = ActiveSupervisor(self)
        self.generation_coordinator = GenerationCoordinator(self)
        from luna_agent.plugins.dependencies import PluginDependencyResolver

        self.dependencies = PluginDependencyResolver(self)
        self._pending_package_removals: dict[str, dict[str, Any]] = {}
        self._plugin_tasks: dict[str, set[asyncio.Task]] = {}
        self._environment_gc_lock = asyncio.Lock()
        self._capability_view: ContextVar[Any | None] = ContextVar(
            f"plugin-capability-view:{id(self)}",
            default=None,
        )
        self.capability_router = CapabilityRouter(
            on_publish=self._activate_snapshot_routes,
            on_retire=self._retire_snapshot,
        )
        self.capability_store = self.capability_router.store
        from luna_agent.plugins.query import PluginQueryService

        self.queries = PluginQueryService(self)

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

    @property
    def _active_owner_running(self) -> bool:
        """Compatibility view; ownership lives in ActiveSupervisor."""
        return self.active_supervisor.owner_running

    @property
    def boot_scope_sealed(self) -> bool:
        return self._boot_scope_sealed

    @property
    def boot_scoped_capability_kinds(self) -> frozenset[CapabilityKind]:
        return BOOT_SCOPED_CAPABILITY_KINDS

    def boot_scope_preserve_kinds(self, plugin_key: str) -> frozenset[CapabilityKind]:
        """Return boot-scoped kinds that must survive a generation change.

        Memory providers are consumed by the process-wide memory manager once it
        is constructed. Platform adapters are slightly different: deferred
        adapters may still be loaded during the first Gateway assembly, so a
        platform kind is frozen only after that plugin already has an active
        route.
        """
        if not self._boot_scope_sealed:
            return frozenset()
        preserved = {CapabilityKind.MEMORY_PROVIDER}
        if any(
            binding.kind is CapabilityKind.PLATFORM
            for binding in self.capability_router.active_bindings.get(plugin_key, ())
        ):
            preserved.add(CapabilityKind.PLATFORM)
        return frozenset(preserved)

    def seal_boot_scope(self) -> None:
        """Freeze capabilities whose consumers are constructed once per process boot."""
        self._boot_scope_sealed = True

    def boot_scope_report(self, plugin_key: str) -> dict[str, Any]:
        pending = self._pending_boot_scope.get(plugin_key, set())
        return {
            "sealed": self._boot_scope_sealed,
            "pending_restart": bool(pending),
            "capabilities": sorted(kind.value for kind in pending),
        }

    def close(self) -> None:
        """Release generation-owned processes for short-lived CLI managers."""
        self.external_runtime.close(self._runtime_records.values())

    async def aclose(self) -> None:
        """Release Workers without blocking the host event loop they call back into."""
        await self.external_runtime.aclose(self._runtime_records.values())

    def bind_application_ports(
        self,
        *,
        conversation_coordinator,
        delivery_service,
        artifact_store=None,
    ) -> None:
        self._conversation_coordinator = conversation_coordinator
        self._delivery_service = delivery_service
        self._artifact_store = artifact_store

    def bind_mcp_manager(self, manager) -> None:
        self._mcp_manager = manager

    def plugin_conversation_port(self, plugin):
        if self._conversation_coordinator is None:
            raise RuntimeError("active plugin runtime is unavailable")
        from luna_agent.plugins.core.ports import PluginConversationPort

        return PluginConversationPort(
            plugin=plugin,
            coordinator=self._conversation_coordinator,
            artifact_store=self._artifact_store,
        )

    def plugin_notification_port(self, plugin, *, capability: str = "notification"):
        if self._conversation_coordinator is None or self._delivery_service is None:
            raise RuntimeError("active plugin runtime is unavailable")
        from luna_agent.plugins.core.ports import PluginNotificationPort

        return PluginNotificationPort(
            plugin=plugin,
            coordinator=self._conversation_coordinator,
            delivery_service=self._delivery_service,
            capability=capability,
        )

    def plugin_resource_facade(self, plugin, request):
        from luna_agent.plugins.active.resources import PluginResourceFacade

        return PluginResourceFacade(manager=self, plugin=plugin, request=request)

    def plugin_llm_port(self, plugin):
        from luna_agent.plugins.core.ports import PluginLLMPort

        return PluginLLMPort(plugin=plugin, settings=self.settings)

    def plugin_event_port(self, plugin):
        if self._event_port_factory is None:
            raise RuntimeError("active plugin event resource is unavailable")
        return self._event_port_factory(plugin)

    def plugin_artifact_port(self, plugin):
        if self._artifact_store is None:
            raise RuntimeError("active plugin artifact resource is unavailable")
        from luna_agent.plugins.core.ports import PluginArtifactPort

        return PluginArtifactPort(plugin=plugin, store=self._artifact_store)

    def plugin_process_port(self, plugin):
        return self.external_runtime.processes.port(plugin)

    def plugin_workspace_port(self, plugin):
        return self.external_runtime.workspaces.port(plugin)

    def plugin_storage_port(self, plugin):
        from luna_agent.plugins.core.ports import PluginStoragePort

        if plugin.active_registration is not None and plugin.data_path is None:
            self.data_revisions.prepare(plugin, candidate=False)
        return PluginStoragePort(plugin=plugin, root=self.installer.data_root)

    def plugin_task_port(self, plugin):
        from luna_agent.plugins.core.ports import PluginTaskPort

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
        enabled_keys = [plugin.key for plugin in self._plugins.values() if plugin.enabled]
        for key in self.dependencies.load_order(enabled_keys):
            plugin = self._plugins[key]
            if not plugin.enabled:
                plugin.status = PluginStatus.DISABLED
                continue
            if plugin.manifest.deferred and not include_deferred:
                if plugin.status not in (PluginStatus.LOADED, PluginStatus.ERROR):
                    plugin.status = PluginStatus.DEFERRED
                continue
            self.load_plugin(plugin.key)

    def load_plugin(self, key: str, *, publish: bool = True) -> LoadedPlugin:
        if not self._plugins:
            self.discover()
        plugin = self._plugins[key]
        if plugin.status == PluginStatus.LOADED:
            return plugin
        if not plugin.enabled:
            plugin.status = PluginStatus.DISABLED
            return plugin

        dependency_report = self.dependencies.report(key)
        if not dependency_report.ok:
            plugin.status = PluginStatus.BLOCKED
            plugin.error = "; ".join(
                item.message for item in dependency_report.issues if item.severity == "error"
            )
            plugin.error_traceback = None
            return plugin
        for requirement in plugin.manifest.requires.plugins:
            dependency = self._plugins.get(requirement.key)
            if dependency is None or not dependency.enabled:
                continue
            if dependency.status is not PluginStatus.LOADED:
                dependency = self.load_plugin(dependency.key, publish=publish)
            if dependency.status is not PluginStatus.LOADED:
                plugin.status = PluginStatus.BLOCKED
                plugin.error = (
                    f"Plugin dependency is not loaded: {dependency.key} "
                    f"({dependency.status.value})"
                )
                plugin.error_traceback = None
                return plugin

        missing_env = self._missing_env(plugin.manifest)
        if missing_env:
            plugin.status = PluginStatus.ERROR
            plugin.error = f"Missing required env: {', '.join(missing_env)}"
            plugin.error_traceback = None
            return plugin

        plugin.status = PluginStatus.LOADING
        self.generation_coordinator.transition(
            plugin,
            PluginRuntimeState.PREPARING,
            reason="load_started",
        )
        plugin.error = None
        plugin.error_traceback = None
        before = self._registration_snapshot()
        data_commit = None
        try:
            plugin.generation_scope = PluginGenerationScope()
            all_config = getattr(self.settings, "plugins_config", {}) or {}
            plugin_config = all_config.get(plugin.key, {}) if isinstance(all_config, dict) else {}
            plugin.package_digest = package_digest(plugin.manifest.path)
            environment = None
            if self._is_external_isolation_enabled(plugin):
                environment = self.external_runtime.prepare_environment(plugin)
            plugin.runtime_backend = (
                RuntimeBackend.WORKER
                if environment is not None
                else RuntimeBackend.IN_PROCESS
            )
            plugin.generation_id = generation_id(
                plugin.key,
                plugin.package_digest,
                plugin_config if isinstance(plugin_config, dict) else {},
                environment_id=environment.environment_id if environment is not None else "",
            )
            plugin.runtime_instance_id = runtime_instance_id(plugin.key)
            if plugin.manifest.source != "builtin":
                plugin.module_namespace = generation_module_namespace(
                    plugin.key,
                    plugin.runtime_instance_id,
                )
            plugin.ctx = PluginRuntimeContext(self, plugin)
            if environment is not None:
                self.data_revisions.prepare(plugin, candidate=True)
                self.external_runtime.start(
                    plugin,
                    environment=environment,
                    config=plugin_config if isinstance(plugin_config, dict) else {},
                )
            else:
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
            if plugin.active_registration is not None:
                plugin.active_enabled = self._resolve_active_enabled(plugin.key)
                if not publish and plugin.data_path is None:
                    self.data_revisions.prepare(plugin, candidate=True)
                plugin.active_runner = self.active_supervisor.create_execution(plugin)
            after = self._registration_snapshot()
            self._assert_no_registry_replacements(before, after, plugin.key, plugin)
            transaction = plugin.registration_transaction
            if transaction is None:
                raise RuntimeError(f"plugin registration transaction is unavailable: {key}")
            if plugin.manifest.record_import_delta:
                transaction.capture_import_delta(before, after)
            self._restore_registration_snapshot(before)
            if publish:
                if environment is not None:
                    data_commit = self.data_revisions.commit(plugin)
                preserve_kinds = self.boot_scope_preserve_kinds(plugin.key)
                transaction.activate(preserve_kinds=preserve_kinds)
                self._publish_plugin_capabilities(
                    plugin,
                    self._registration_snapshot(),
                    publish=True,
                )
                self._record_boot_scope_pending(
                    plugin,
                    preserve_kinds=preserve_kinds,
                )
                transaction.finalize()
                if data_commit is not None:
                    data_commit.finalize()
            else:
                self._publish_plugin_capabilities(plugin, before, publish=False)
            plugin.status = PluginStatus.LOADED
            if publish:
                self.generation_coordinator.transition(
                    plugin,
                    PluginRuntimeState.ACTIVE,
                    reason="initial_generation_published",
                )
            self._runtime_records[plugin.runtime_instance_id] = plugin
            if publish:
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
            transaction = plugin.registration_transaction
            if transaction is not None:
                transaction.rollback()
            if data_commit is not None and not data_commit.finalized:
                try:
                    data_commit.rollback()
                except Exception:
                    logger.exception("Failed to roll back plugin data revision: %s", key)
            self.external_runtime.stop(plugin)
            self.data_revisions.discard(plugin)
            self.capability_router.discard_runtime(plugin.runtime_instance_id)
            self.hook_manager.unregister_owner(plugin.runtime_instance_id)
            self._restore_registration_snapshot(before)
            self._clear_plugin_registrations(plugin)
            self._close_generation_scope(plugin)
            cleanup_generation_namespace(plugin.module_namespace)
            plugin.module = None
            plugin.ctx = None
            plugin.status = PluginStatus.ERROR
            self.generation_coordinator.transition(
                plugin,
                PluginRuntimeState.FAILED,
                reason="load_failed",
            )
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
        self.generation_coordinator.transition(
            plugin,
            PluginRuntimeState.DRAINING,
            reason="unload_requested",
        )
        self._remove_plugin_commands(key)
        self._remove_plugin_hooks(key)
        from luna_agent.platforms.core import platform_registry
        from luna_agent.skills.registry import skill_registry
        from luna_agent.tools.registry import tool_registry
        from luna_agent.workflow.registry import workflow_registry
        from luna_agent.memory.provider_registry import memory_provider_registry

        for name in list(plugin.tools_registered):
            tool_registry.unregister(name)
        for name in list(plugin.skills_registered):
            skill_registry.unregister(name)
        for name in list(plugin.workflows_registered):
            workflow_registry.unregister(name)
        preserve_kinds = self.boot_scope_preserve_kinds(plugin.key)
        self._record_boot_scope_pending(
            plugin,
            preserve_kinds=preserve_kinds,
        )
        if CapabilityKind.PLATFORM not in preserve_kinds:
            for name in list(plugin.platforms_registered):
                platform_registry.unregister(name)

        self.mcp_server_registry.unregister_plugin(key)
        if CapabilityKind.MEMORY_PROVIDER not in preserve_kinds:
            memory_provider_registry.unregister_plugin(key)
        self._clear_plugin_registrations(plugin)
        plugin.module = None
        plugin.ctx = None
        plugin.error = None
        plugin.error_traceback = None
        plugin.status = PluginStatus.DISABLED if not plugin.enabled else PluginStatus.DISCOVERED
        self._publish_without_owner(plugin.key)
        self._active_runtime_by_plugin.pop(plugin.key, None)
        if plugin.runtime_instance_id not in self.capability_store.retained_runtime_ids():
            self.external_runtime.stop(plugin)
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
        publish: bool = True,
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
        loaded = self.load_plugin(manifest.key, publish=publish)
        if loaded.status != PluginStatus.LOADED:
            if previous is not None:
                self._plugins[manifest.key] = previous
                self.generation_coordinator.transition(
                    previous,
                    PluginRuntimeState.ACTIVE,
                    reason="candidate_prepare_failed",
                )
            return loaded
        if previous is not None and not publish:
            self._plugins[manifest.key] = previous
        if previous is not None and publish:
            self.generation_coordinator.transition(
                previous,
                PluginRuntimeState.DRAINING,
                reason="synchronous_reload_published",
            )
        return loaded

    async def reload_plugin_runtime(self, key: str) -> LoadedPlugin:
        async with self.operations.track(key, "reload"):
            return await self._reload_plugin_runtime(key)

    async def _reload_plugin_runtime(self, key: str) -> LoadedPlugin:
        self.operations.stage("preparing")
        previous = self._plugins.get(key)
        if previous is None or previous.status is not PluginStatus.LOADED:
            return self.load_plugin(key)
        candidate = self._activate_manifest(
            previous.manifest,
            previous=previous,
            evict=True,
            publish=False,
        )
        if candidate.status is not PluginStatus.LOADED:
            return candidate
        return await self._commit_staged_plugin(candidate, previous)

    async def _commit_staged_plugin(
        self,
        candidate: LoadedPlugin,
        previous: LoadedPlugin,
    ) -> LoadedPlugin:
        previous_quiesced = False
        data_commit = None
        publication = None
        transaction = candidate.registration_transaction
        if transaction is None:
            raise RuntimeError(
                f"plugin registration transaction is unavailable: {candidate.key}"
            )
        try:
            if self._mcp_manager is not None:
                self.operations.stage("waiting_mcp")
                await self._mcp_manager.reconcile(
                    self._desired_mcp_configs(candidate=candidate)
                )
            should_start = (
                self._active_owner_running
                and candidate.active_enabled
                and candidate.active_runner is not None
            )
            if should_start:
                self.operations.stage("waiting_ready")
                old_runner = previous.active_runner
                if (
                    old_runner is not None
                    and old_runner.root_task is not None
                    and not old_runner.root_task.done()
                ):
                    await old_runner.quiesce()
                    previous_quiesced = True
                await self._wait_required_mcp(candidate)
                candidate.active_runner.start()
                await candidate.active_runner.wait_ready()

            if candidate.data_revision_id:
                data_commit = self.data_revisions.commit(candidate)
            self.operations.stage("publishing")
            publication = self.generation_coordinator.publish_candidate(
                candidate,
                previous,
                data_commit=data_commit,
            )
            if should_start:
                candidate.active_runner.control.commit()
                self._watch_active_plugin(candidate, candidate.active_runner)
            publication.finalize()
        except Exception:
            if publication is not None:
                publication.rollback()
            else:
                transaction.rollback()
                if data_commit is not None and not data_commit.finalized:
                    data_commit.rollback()
            await self._discard_staged_plugin(candidate, previous)
            if previous_quiesced and previous.active_runner is not None:
                await previous.active_runner.resume()
            if self._mcp_manager is not None:
                await self._mcp_manager.reconcile(self._desired_mcp_configs())
            raise
        try:
            self.events.record(
                candidate.key,
                "generation_published",
                operation_id=self.operations.current_operation_id(),
                details={
                    "generation_id": candidate.generation_id,
                    "runtime_instance_id": candidate.runtime_instance_id,
                },
            )
        except Exception:
            logger.exception(
                "Failed to record plugin generation publication: %s",
                candidate.key,
            )
        if previous.active_runner is not None:
            self.operations.stage("draining")
            await self._stop_active_plugin(previous)
        return candidate

    async def _discard_staged_plugin(
        self,
        candidate: LoadedPlugin,
        previous: LoadedPlugin,
    ) -> None:
        runner = candidate.active_runner
        if runner is not None:
            runner.control.abort(candidate.active_error or "candidate activation failed")
            await runner.stop()
        self.data_revisions.discard(candidate)
        self.capability_router.discard_runtime(candidate.runtime_instance_id)
        self._runtime_records.pop(candidate.runtime_instance_id, None)
        self.hook_manager.unregister_owner(candidate.runtime_instance_id)
        scope = candidate.generation_scope
        if scope is not None:
            await scope.aclose()
        cleanup_generation_namespace(candidate.module_namespace)
        self.external_runtime.stop(candidate)
        candidate.module = None
        candidate.ctx = None
        self.generation_coordinator.transition(
            candidate,
            PluginRuntimeState.FAILED,
            reason="candidate_discarded",
        )
        self._plugins[previous.key] = previous
        self.generation_coordinator.transition(
            previous,
            PluginRuntimeState.ACTIVE,
            reason="candidate_rollback",
        )
        self._active_runtime_by_plugin[previous.key] = previous.runtime_instance_id
        old_bindings = self.capability_router.runtime_bindings.get(
            previous.runtime_instance_id, ()
        )
        self._activate_binding_payloads(old_bindings)

    async def unload_plugin_runtime(self, key: str) -> LoadedPlugin:
        current = self._plugins.get(key)
        if current is not None:
            await self._stop_active_plugin(current)
        plugin = self.unload_plugin(key)
        if self._mcp_manager is not None:
            await self._mcp_manager.reconcile(self._desired_mcp_configs())
        return plugin

    async def enable_plugin_runtime(self, key: str) -> LoadedPlugin:
        async with self.operations.track(key, "enable"):
            return await self._enable_plugin_runtime(key)

    async def _enable_plugin_runtime(self, key: str) -> LoadedPlugin:
        self.operations.stage("preparing")
        plugin = self.enable_plugin(key)
        self.install_store.set_enabled(key, True)
        loaded = self.load_plugin(plugin.key)
        if self._mcp_manager is not None:
            await self._mcp_manager.reconcile(self._desired_mcp_configs())
        if self._active_owner_running and loaded.active_enabled:
            await self._start_active_plugin(loaded)
        return loaded

    async def disable_plugin_runtime(self, key: str) -> LoadedPlugin:
        async with self.operations.track(key, "disable"):
            return await self._disable_plugin_runtime(key)

    async def _disable_plugin_runtime(self, key: str) -> LoadedPlugin:
        self.operations.stage("draining")
        plugin = self._plugins[key]
        plugin.enabled = False
        self._state.setdefault("disabled", [])
        if key not in self._state["disabled"]:
            self._state["disabled"].append(key)
        self._state["enabled"] = [item for item in self._state.get("enabled", []) if item != key]
        self._save_state()
        self.install_store.set_enabled(key, False)
        return await self.unload_plugin_runtime(key)

    async def start_active_plugins(self) -> None:
        """Start enabled active runners. Gateway is the sole owner of this call."""
        await self.active_supervisor.start_all()

    async def stop_active_plugins(self) -> None:
        await self.active_supervisor.stop_all()

    async def set_active_enabled(self, key: str, enabled: bool) -> LoadedPlugin:
        action = "active_enable" if enabled else "active_disable"
        async with self.operations.track(key, action):
            return await self._set_active_enabled(key, enabled)

    async def _set_active_enabled(self, key: str, enabled: bool) -> LoadedPlugin:
        plugin = self._plugins[key]
        if plugin.active_registration is None:
            raise ValueError(f"plugin does not register an active runner: {key}")
        plugin.active_enabled = bool(enabled)
        self._state.setdefault("active_enabled", [])
        self._state.setdefault("active_disabled", [])
        target = "active_enabled" if enabled else "active_disabled"
        opposite = "active_disabled" if enabled else "active_enabled"
        if key not in self._state[target]:
            self._state[target].append(key)
        self._state[opposite] = [item for item in self._state[opposite] if item != key]
        self._save_state()
        if self.active_supervisor.owner_running:
            if enabled:
                await self.active_supervisor.start(plugin)
            else:
                await self.active_supervisor.stop(plugin)
        return plugin

    async def restart_active_plugin(self, key: str) -> LoadedPlugin:
        async with self.operations.track(key, "active_restart"):
            return await self._restart_active_plugin(key)

    async def trigger_active_plugin(
        self,
        key: str,
        *,
        reason: str = "manual",
    ) -> LoadedPlugin:
        """Wake the current active generation without reloading it."""
        plugin = self._plugins.get(key)
        if plugin is None:
            raise KeyError(f"unknown plugin: {key}")
        self.active_supervisor.trigger(plugin, reason)
        self.events.record(
            key,
            "active_wakeup_requested",
            details={"reason": str(reason or "manual")},
        )
        return plugin

    async def _restart_active_plugin(self, key: str) -> LoadedPlugin:
        self.operations.stage("draining")
        plugin = self._plugins[key]
        if not plugin.active_enabled:
            raise ValueError(f"active plugin is disabled: {key}")
        if self.active_supervisor.owner_running:
            self.operations.stage("waiting_ready")
        await self.active_supervisor.restart(plugin)
        return plugin

    async def _start_active_plugin(self, plugin: LoadedPlugin) -> None:
        await self.active_supervisor.start(plugin)

    async def _stop_active_plugin(self, plugin: LoadedPlugin) -> None:
        await self.active_supervisor.stop(plugin)

    def _watch_active_plugin(self, plugin: LoadedPlugin, runner) -> None:
        self.active_supervisor.watch(plugin, runner)

    def _active_restart_delays(self, key: str) -> tuple[float, ...]:
        return self.active_supervisor.restart_delays(key)

    async def _wait_required_mcp(self, plugin: LoadedPlugin) -> None:
        await self.active_supervisor.wait_required_mcp(plugin)

    async def install_plugin_runtime(
        self,
        source: Path | str,
        *,
        enable: bool = True,
    ) -> LoadedPlugin:
        package = self.installer.prepare(source)
        async with self.operations.track(package.manifest.key, "install"):
            return await self._install_prepared_plugin(package, enable=enable)

    async def _install_prepared_plugin(
        self,
        package,
        *,
        enable: bool,
    ) -> LoadedPlugin:
        self.operations.stage("validating", details={"source": package.source})
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
            self.events.record(
                plugin.key,
                "installed",
                operation_id=self.operations.current_operation_id(),
                details={"version": plugin.manifest.version, "enabled": False},
            )
            return plugin
        try:
            self.operations.stage("preparing")
            if previous is not None and previous.status is PluginStatus.LOADED:
                candidate = self._activate_manifest(
                    package.manifest,
                    previous=previous,
                    evict=True,
                    force_enabled=enable,
                    publish=False,
                )
                if candidate.status is not PluginStatus.LOADED:
                    raise RuntimeError(candidate.error or f"Plugin failed to load: {candidate.key}")
                loaded = await self._commit_staged_plugin(candidate, previous)
            else:
                loaded = self._activate_manifest(
                    package.manifest,
                    previous=previous,
                    evict=previous is not None,
                    force_enabled=enable,
                )
            if loaded.status != PluginStatus.LOADED:
                raise RuntimeError(loaded.error or f"Plugin failed to load: {loaded.key}")
            if self._mcp_manager is not None:
                self.operations.stage("waiting_mcp")
                await self._mcp_manager.reconcile(self._desired_mcp_configs())
            if previous is None and self._active_owner_running and loaded.active_enabled:
                await self._start_active_plugin(loaded)
        except Exception:
            if (
                previous is not None
                and previous.runtime_instance_id
                and self._plugins.get(previous.key) is not previous
            ):
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
        self.events.record(
            loaded.key,
            "installed",
            operation_id=self.operations.current_operation_id(),
            details={
                "version": loaded.manifest.version,
                "package_digest": package.digest,
                "enabled": enable,
            },
        )
        return loaded

    async def rollback_plugin_runtime(self, key: str, digest: str) -> LoadedPlugin:
        async with self.operations.track(key, "rollback"):
            return await self._rollback_plugin_runtime(key, digest)

    async def _rollback_plugin_runtime(self, key: str, digest: str) -> LoadedPlugin:
        self.operations.stage("preparing", details={"package_digest": digest})
        path = self.install_store.package_path(key, digest)
        manifest_path = self._resolve_plugin_manifest_path(path)
        manifest = PluginManifest.from_mapping(
            self._read_manifest_file(manifest_path),
            source="installed",
            path=path,
        )
        previous = self._plugins.get(key)
        if previous is not None and previous.status is PluginStatus.LOADED:
            candidate = self._activate_manifest(
                manifest,
                previous=previous,
                evict=True,
                publish=False,
            )
            if candidate.status is not PluginStatus.LOADED:
                self._rollback_to_runtime(key, previous.runtime_instance_id)
                raise RuntimeError(candidate.error or f"Plugin rollback failed: {key}")
            loaded = await self._commit_staged_plugin(candidate, previous)
        else:
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
        self.events.record(
            key,
            "rolled_back",
            operation_id=self.operations.current_operation_id(),
            details={"package_digest": digest, "version": loaded.manifest.version},
        )
        return loaded

    async def uninstall_plugin_runtime(
        self,
        key: str,
        *,
        purge_data: bool = False,
        force: bool = False,
    ) -> LoadedPlugin:
        async with self.operations.track(key, "uninstall"):
            return await self._uninstall_plugin_runtime(
                key,
                purge_data=purge_data,
                force=force,
            )

    async def _uninstall_plugin_runtime(
        self,
        key: str,
        *,
        purge_data: bool,
        force: bool,
    ) -> LoadedPlugin:
        self.operations.stage("draining", details={"purge_data": purge_data})
        dependents = self.dependencies.dependents(key, enabled_only=True)
        if dependents and not force:
            raise RuntimeError(
                "Plugin has enabled dependents: " + ", ".join(dependents)
            )
        for dependent_key in dependents:
            self.disable_plugin(dependent_key)
            self.install_store.set_enabled(dependent_key, False)
            self.events.record(
                dependent_key,
                "dependency_disabled",
                details={"dependency": key},
            )
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
        self.events.record(
            key,
            "uninstalled",
            operation_id=self.operations.current_operation_id(),
            details={"purge_data": purge_data},
        )
        return plugin

    def capability_payload(self, binding_id: str) -> Any | None:
        return self.capability_router.payload(binding_id)

    def plugin_environment_report(self) -> dict[str, Any]:
        retained: dict[tuple[str, str], set[str]] = {}
        conservative_keys: set[str] = set()

        def retain(key: str, environment_id: str, reason: str) -> None:
            retained.setdefault((str(key), str(environment_id)), set()).add(str(reason))

        for plugin in self._runtime_records.values():
            environment_id = str(getattr(plugin, "environment_id", "") or "")
            if environment_id:
                retain(plugin.key, environment_id, "runtime_generation")

        for key, record in self.install_store.packages().items():
            versions = record.get("versions", {}) if isinstance(record, dict) else {}
            for digest, item in versions.items():
                if not isinstance(item, dict) or not item.get("path"):
                    continue
                try:
                    manifest_path = self._resolve_plugin_manifest_path(Path(item["path"]))
                    manifest = PluginManifest.from_mapping(
                        self._read_manifest_file(manifest_path),
                        source="installed",
                        path=Path(item["path"]),
                    )
                    environment_id = self.external_runtime.environments.environment_id(
                        str(key), manifest.requires.python
                    )
                    retain(str(key), environment_id, f"installed_package:{digest}")
                except Exception:
                    conservative_keys.add(str(key))

        report = self.external_runtime.environments.collect_garbage(
            retained=retained,
            retain_plugin_keys=conservative_keys,
            dry_run=True,
        )
        report["conservative_plugin_keys"] = sorted(conservative_keys)
        return report

    async def gc_plugin_environments(self, *, dry_run: bool = True) -> dict[str, Any]:
        async with self._environment_gc_lock:
            async with self.operations.track("__plugin_runtime__", "environment_gc"):
                self.operations.stage("scanning", details={"dry_run": bool(dry_run)})
                report = self.plugin_environment_report()
                if not dry_run:
                    retained: dict[tuple[str, str], set[str]] = {}
                    conservative_keys: set[str] = set()
                    for item in report.get("retained", []):
                        retained.setdefault(
                            (str(item.get("plugin_key") or ""), str(item.get("environment_id") or "")),
                            set(),
                        ).update(item.get("reasons") or [])
                    conservative_keys.update(report.get("conservative_plugin_keys") or [])
                    # Recompute from the same source of truth immediately before
                    # deletion; this keeps apply mode conservative if a generation
                    # changed while a dry-run report was being inspected.
                    fresh = self.plugin_environment_report()
                    for item in fresh.get("retained", []):
                        retained.setdefault(
                            (str(item.get("plugin_key") or ""), str(item.get("environment_id") or "")),
                            set(),
                        ).update(item.get("reasons") or [])
                    conservative_keys.update(fresh.get("conservative_plugin_keys") or [])
                    report = self.external_runtime.environments.collect_garbage(
                        retained=retained,
                        retain_plugin_keys=conservative_keys,
                        dry_run=False,
                    )
                self.operations.stage("completed")
                return report

    def refresh_mcp_tools(
        self,
        server_name: str,
        runtime_instance_id: str,
        tool_names: set[str],
    ) -> None:
        from luna_agent.tools.registry import tool_registry

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
            binding = self.capability_router.mapper.binding(
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
        self.capability_router.replace_dynamic_source(
            f"mcp:{server_name}",
            bindings,
            payloads,
        )

    @contextmanager
    def bind_capability_view(self, view):
        from luna_agent.skills.registry import skill_registry
        from luna_agent.workflow.registry import workflow_registry

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

        report = self.queries.plugin_info(plugin.key, check_entrypoint=True)
        report["validation_path"] = str(plugin_dir)
        report["validation_manifest"] = str(manifest_path)
        report["validation_load_requested"] = load
        report["validation_loaded"] = report["status"] == PluginStatus.LOADED.value
        report["validation_ok"] = (
            report["manifest_valid"]
            and report["entrypoint_importable"]
            and not report["missing_env"]
            and report["status"] not in {
                PluginStatus.ERROR.value,
                PluginStatus.BLOCKED.value,
            }
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
            sources = {existing.manifest.source, manifest.source}
            if sources == {"local", "installed"}:
                if existing.manifest.source == "installed":
                    return
                if existing.status not in {PluginStatus.LOADING, PluginStatus.LOADED}:
                    del self._plugins[manifest.key]
                else:
                    existing.status = PluginStatus.ERROR
                    existing.error = (
                        f"Installed plugin conflicts with loaded local source: {manifest.key}"
                    )
                    existing.error_traceback = None
                    return
            else:
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
        external = self.external_runtime.summary(plugin)
        worker = external.get("worker") or {}
        if external.get("isolated") and not worker.get("running"):
            detail = str(worker.get("last_error") or worker.get("stderr_tail") or "").strip()
            hints.append(
                "外置 Worker 已停止"
                + (f": {detail[-500:]}" if detail else "")
            )
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
        if declared != manifest.source and not (
            manifest.source == "installed" and declared == "local"
        ):
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

    def _resolve_active_enabled(self, key: str) -> bool:
        enabled = set(self._state.get("active_enabled", []))
        disabled = set(self._state.get("active_disabled", []))
        if key in disabled:
            return False
        if key in enabled:
            return True
        all_config = getattr(self.settings, "plugins_config", {}) or {}
        plugin_config = all_config.get(key, {}) if isinstance(all_config, dict) else {}
        active = plugin_config.get("active", {}) if isinstance(plugin_config, dict) else {}
        return bool(active.get("enabled", False)) if isinstance(active, dict) else False

    def _load_state(self) -> dict[str, list[str]]:
        data = read_json_object(
            self._state_path,
            {"enabled": [], "disabled": [], "active_enabled": [], "active_disabled": []},
        )
        return {
            "enabled": list(data.get("enabled", [])),
            "disabled": list(data.get("disabled", [])),
            "active_enabled": list(data.get("active_enabled", [])),
            "active_disabled": list(data.get("active_disabled", [])),
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

    def _is_external_isolation_enabled(self, plugin: LoadedPlugin) -> bool:
        return bool(
            plugin.manifest.source != "builtin"
            and getattr(self.settings, "plugin_worker_isolation", False)
        )

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
        from luna_agent.platforms.core import platform_registry
        from luna_agent.skills.registry import skill_registry
        from luna_agent.tools.registry import tool_registry
        from luna_agent.workflow.registry import workflow_registry

        return {
            "tools": tool_registry.all_names,
            "skills": {entry.name for entry in skill_registry.list()},
            "workflows": set(workflow_registry.list_names()),
            "platforms": {entry.name for entry in platform_registry.list()},
        }

    def _registration_snapshot(self) -> dict[str, Any]:
        from luna_agent.memory.provider_registry import memory_provider_registry
        from luna_agent.platforms.core import platform_registry
        from luna_agent.skills.registry import skill_registry
        from luna_agent.tools.registry import tool_registry
        from luna_agent.workflow.registry import workflow_registry

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

    def _publish_plugin_capabilities(
        self,
        plugin: LoadedPlugin,
        snapshot: dict[str, Any],
        *,
        publish: bool = True,
    ) -> None:
        bindings: list[Any] = []
        payloads: dict[str, Any] = {}

        def add(kind, name, payload, *, manager_key=None, contract=None, metadata=None, ordinal=0):
            binding = self.capability_router.mapper.binding(
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
        transaction = plugin.registration_transaction
        for name in plugin.tools_registered:
            entry = (
                transaction.named("tools", name) if transaction is not None else None
            ) or entries["tools"].get(name)
            if entry is not None:
                add(CapabilityKind.TOOL, name, entry, contract=_tool_contract(entry))
        for name in plugin.skills_registered:
            entry = (
                transaction.named("skills", name) if transaction is not None else None
            ) or entries["skills"].get(name)
            if entry is not None:
                add(CapabilityKind.SKILL, name, entry, contract=_skill_contract(entry))
        for name in plugin.workflows_registered:
            entry = (
                transaction.named("workflows", name) if transaction is not None else None
            ) or entries["workflows"].get(name)
            if entry is not None:
                add(CapabilityKind.WORKFLOW, name, entry, contract=_workflow_contract(entry))
        for name in plugin.platforms_registered:
            entry = (
                transaction.named("platforms", name) if transaction is not None else None
            ) or entries["platforms"].get(name)
            if entry is not None:
                add(CapabilityKind.PLATFORM, name, entry, contract=_platform_contract(entry))
        for name in plugin.memory_providers_registered:
            entry = (
                transaction.memory_providers.get(name)
                if transaction is not None
                else None
            ) or entries["memory_providers"].get(name)
            if entry is not None:
                add(CapabilityKind.MEMORY_PROVIDER, name, entry, contract=name)
        for name in plugin.commands_registered:
            entry = (
                transaction.commands.get(name) if transaction is not None else None
            ) or self._commands.get(name)
            if entry is not None:
                add(
                    CapabilityKind.COMMAND,
                    name,
                    entry,
                    contract={"description": entry.description, "scope": entry.scope},
                )

        typed_hooks = (
            list(transaction.typed_hooks)
            if transaction is not None
            else [
                item for item in self.hook_manager.registrations(include_inactive=True)
                if item.owner == plugin.runtime_instance_id
            ]
        )
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
                    "order": getattr(registration, "order", ordinal),
                    "matcher": registration.matcher,
                },
                ordinal=ordinal,
            )
        mcp_entries, _revision = self.mcp_server_registry.snapshot()
        for name in plugin.mcp_servers_registered:
            registration = (
                transaction.mcp_servers.get(name)
                if transaction is not None
                else None
            ) or mcp_entries.get(name)
            if registration is not None:
                add(
                    CapabilityKind.MCP_SERVER,
                    name,
                    registration,
                    contract=registration.config,
                )

        core_bindings, core_payloads = self._capture_core_bindings(snapshot, plugin)
        self.capability_router.stage(plugin.runtime_instance_id, bindings, payloads)
        if not publish:
            return
        self.capability_router.publish_plugin(
            owner=plugin.key,
            runtime_instance_id=plugin.runtime_instance_id,
            core_bindings=core_bindings,
            core_payloads=core_payloads,
            preserve_kinds=self.boot_scope_preserve_kinds(plugin.key),
        )

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
                payload_owner = str(
                    getattr(payload, "_plugin_key", "")
                    or getattr(payload, "plugin_key", "")
                    or ""
                )
                if payload_owner:
                    continue
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
                binding = self.capability_router.mapper.binding(
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
        self.capability_router.publish_without_owner(
            owner,
            preserve_kinds=self.boot_scope_preserve_kinds(owner),
        )

    def _record_boot_scope_pending(
        self,
        plugin: LoadedPlugin,
        *,
        preserve_kinds: Iterable[CapabilityKind] = (),
    ) -> None:
        preserved = set(preserve_kinds)
        if not preserved:
            return
        candidate = self.capability_router.runtime_bindings.get(
            plugin.runtime_instance_id,
            (),
        )
        live = self.capability_router.active_bindings.get(plugin.key, ())
        kinds = {
            binding.kind
            for binding in (*candidate, *live)
            if binding.kind in preserved
        }
        if kinds:
            self._pending_boot_scope.setdefault(plugin.key, set()).update(kinds)

    def _publish_current_bindings(self) -> None:
        self.capability_router.publish_current()

    def _activate_snapshot_routes(self, snapshot) -> None:
        active_hook_ids = {
            route.manager_key
            for routes in snapshot.routes.get(CapabilityKind.HOOK, {}).values()
            for route in routes
        }
        self.hook_manager.activate_managed_routes(active_hook_ids)

    def _retire_snapshot(self, snapshot) -> None:
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
            self._close_generation_scope(plugin)
            self.external_runtime.stop(plugin)
            cleanup_generation_namespace(plugin.module_namespace)
            plugin.ctx = None
            plugin.module = None
            self.generation_coordinator.transition(
                plugin,
                PluginRuntimeState.STOPPED,
                reason="snapshot_retired",
            )
            self.capability_router.discard_runtime(runtime_id)
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

    @staticmethod
    def _close_generation_scope(plugin: LoadedPlugin) -> None:
        scope = plugin.generation_scope
        if scope is None or scope.closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(scope.aclose())
        else:
            loop.create_task(
                scope.aclose(),
                name=f"plugin-scope-close:{plugin.runtime_instance_id or plugin.key}",
            )

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

    def _desired_mcp_configs(self, *, candidate: LoadedPlugin | None = None) -> list[Any]:
        plugin_configs: list[Any]
        if candidate is None or candidate.registration_transaction is None:
            plugin_configs = self.get_mcp_servers()
        else:
            entries, _revision = self.mcp_server_registry.snapshot()
            plugin_configs = [
                registration.config
                for registration in entries.values()
                if registration.plugin_key != candidate.key
            ]
            plugin_configs.extend(candidate.registration_transaction.mcp_configs())
        return [
            *list(getattr(self.settings, "mcp_servers", []) or []),
            *plugin_configs,
        ]

    def _rollback_to_runtime(self, plugin_key: str, runtime_id: str) -> None:
        previous = self._runtime_records.get(runtime_id)
        bindings = self.capability_router.runtime_bindings.get(runtime_id)
        if previous is None or bindings is None:
            return
        current_id = self._active_runtime_by_plugin.get(plugin_key, "")
        current = self._runtime_records.get(current_id)
        if current is not None:
            self.generation_coordinator.transition(
                current,
                PluginRuntimeState.DRAINING,
                reason="rollback_replaced_current",
            )
        self._plugins[plugin_key] = previous
        self.generation_coordinator.transition(
            previous,
            PluginRuntimeState.ACTIVE,
            reason="rollback_restored_runtime",
        )
        previous.status = PluginStatus.LOADED
        self._active_runtime_by_plugin[plugin_key] = runtime_id
        self._activate_binding_payloads(bindings)
        self.capability_router.restore_owner(
            plugin_key,
            runtime_id,
            preserve_kinds=self.boot_scope_preserve_kinds(plugin_key),
        )

    def _activate_binding_payloads(self, bindings) -> None:
        from luna_agent.memory.provider_registry import memory_provider_registry
        from luna_agent.platforms.core import platform_registry
        from luna_agent.skills.registry import skill_registry
        from luna_agent.tools.registry import tool_registry
        from luna_agent.workflow.registry import workflow_registry

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
                preserve_kinds = self.boot_scope_preserve_kinds(binding.owner)
                if CapabilityKind.PLATFORM not in preserve_kinds:
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
                preserve_kinds = self.boot_scope_preserve_kinds(binding.owner)
                if CapabilityKind.MEMORY_PROVIDER not in preserve_kinds:
                    memory_provider_registry.register(
                        name=payload.name,
                        plugin_key=payload.plugin_key,
                        factory=payload.factory,
                        validator=payload.validator,
                    )

    def _restore_registration_snapshot(self, snapshot: dict[str, Any]) -> None:
        from luna_agent.memory.provider_registry import memory_provider_registry
        from luna_agent.platforms.core import platform_registry
        from luna_agent.skills.registry import skill_registry
        from luna_agent.tools.registry import tool_registry
        from luna_agent.workflow.registry import workflow_registry

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
        plugin: LoadedPlugin | None = None,
    ) -> None:
        registered_names = {
            "tools": set(getattr(plugin, "tools_registered", ())) if plugin else set(),
            "skills": set(getattr(plugin, "skills_registered", ())) if plugin else set(),
            "workflows": set(getattr(plugin, "workflows_registered", ())) if plugin else set(),
            "platforms": set(getattr(plugin, "platforms_registered", ())) if plugin else set(),
            "memory_providers": (
                set(getattr(plugin, "memory_providers_registered", ()))
                if plugin
                else set()
            ),
        }
        for kind, previous in before["entries"].items():
            current = after["entries"].get(kind, {})
            for name, entry in previous.items():
                if name in current and current[name] is not entry:
                    if name in registered_names.get(kind, set()):
                        continue
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
