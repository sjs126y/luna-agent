"""Read-only queries over plugin runtime state."""

from __future__ import annotations

from typing import Any

from personal_agent.plugins.core.models import PluginStatus


class PluginQueryService:
    """Stable read-only views attached to the owning PluginManager."""

    def __init__(self, manager) -> None:
        self._manager = manager

    def list_plugins(self) -> list[dict[str, Any]]:
        if not self._manager._plugins:
            self._manager.discover()
        return [self.plugin_info(plugin.key) for plugin in self._manager.list_plugins()]

    def plugin_info(
        self,
        key: str,
        *,
        check_entrypoint: bool | None = None,
    ) -> dict[str, Any]:
        manager = self._manager
        if not manager._plugins:
            manager.discover()
        plugin = manager._plugins[key]
        missing_env = manager._missing_env(plugin.manifest)
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
            entrypoint_ok, entrypoint_error = manager._check_entrypoint(plugin.manifest)
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
            "snapshot_revision": manager.capability_store.current.revision,
            "active_enabled": plugin.active_enabled,
            "active": (
                plugin.active_runner.control.safe_summary()
                if plugin.active_runner is not None
                else {}
            ),
            "active_error": plugin.active_error,
            "active_restart_count": plugin.active_restart_count,
            "active_circuit_open": plugin.active_circuit_open,
            "active_resources": (
                plugin.active_registration.resources.safe_summary()
                if plugin.active_registration is not None
                else {}
            ),
            "package_digest": plugin.package_digest,
            "deferred": plugin.deferred,
            "source": plugin.manifest.source,
            "declared_source": plugin.manifest.declared_source or plugin.manifest.source,
            "path": str(plugin.manifest.path) if plugin.manifest.path else "",
            "manifest_path": manager._manifest_path(plugin),
            "source_boundary": manager._source_boundary(plugin),
            "requires_env": plugin.manifest.requires_env,
            "missing_env": missing_env,
            "manifest_valid": not manifest_error,
            "manifest_error": manifest_error,
            "manifest_unknown_fields": list(plugin.manifest.unknown_fields),
            "manifest_warnings": manager._manifest_warnings(plugin),
            "boundary_warnings": manager._boundary_warnings(plugin),
            "entrypoint_checked": entrypoint_checked,
            "entrypoint_importable": entrypoint_ok,
            "entrypoint_error": entrypoint_error,
            "deferred_reason": manager._deferred_reason(plugin),
            "error": plugin.error or "",
            "error_traceback": plugin.error_traceback or "",
            "registered": plugin.registration_counts(),
            "registered_items": manager._registered_items(plugin),
            "diagnostic_hints": manager._diagnostic_hints(
                plugin,
                missing_env,
                entrypoint_ok,
                entrypoint_error,
            ),
        }

    def runtime_health(self) -> dict[str, Any]:
        manager = self._manager
        data = manager.capability_store.health_snapshot()
        runtime_counts: dict[str, int] = {}
        for plugin in manager._runtime_records.values():
            state = plugin.runtime_state.value
            runtime_counts[state] = runtime_counts.get(state, 0) + 1
        data.update({
            "active_plugin_owners": sorted(
                owner for owner in manager._active_bindings if owner != "core"
            ),
            "payload_count": len(manager._binding_payloads),
            "runtime_counts": runtime_counts,
            "install_revision": manager.install_store.revision,
            "installed_packages": len(manager.install_store.packages()),
            "pending_removals": sorted(manager._pending_package_removals),
            "active_owner_running": manager._active_owner_running,
            "active_plugins": [
                {
                    "key": plugin.key,
                    "enabled": plugin.active_enabled,
                    "error": plugin.active_error,
                    "restart_count": plugin.active_restart_count,
                    "circuit_open": plugin.active_circuit_open,
                    **(
                        plugin.active_runner.control.safe_summary()
                        if plugin.active_runner is not None
                        else {"state": "unavailable"}
                    ),
                }
                for plugin in manager._plugins.values()
                if plugin.active_registration is not None
            ],
        })
        return data

