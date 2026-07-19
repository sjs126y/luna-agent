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
        report = {
            "management_schema_version": 1,
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
            "plugin_api": plugin.manifest.plugin_api,
            "requires": plugin.manifest.requires.as_dict(),
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
        operations = self.operations(key=plugin.key, limit=1)
        events = self.events(plugin.key, limit=1)
        report["latest_operation"] = operations[0] if operations else {}
        report["latest_event"] = events[0] if events else {}
        report["installed_versions"] = self.versions(plugin.key)
        report["mcp"] = self._mcp_health(plugin.mcp_servers_registered)
        report["dependency_report"] = manager.dependencies.report(plugin.key).as_dict()
        return report

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

    def versions(self, key: str) -> list[dict[str, Any]]:
        record = self._manager.install_store.packages().get(key, {})
        active = str(record.get("active_package") or "") if isinstance(record, dict) else ""
        versions = record.get("versions", {}) if isinstance(record, dict) else {}
        result = []
        for digest, item in versions.items():
            if not isinstance(item, dict):
                continue
            result.append({
                "plugin_key": key,
                "digest": str(digest),
                "version": str(item.get("version") or ""),
                "source": str(item.get("source") or ""),
                "path": str(item.get("path") or ""),
                "active": str(digest) == active,
                "status": str(record.get("status") or ""),
            })
        return sorted(result, key=lambda item: (not item["active"], item["version"], item["digest"]))

    def events(self, key: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return self._manager.events.list(key, limit=limit)

    def operations(self, *, key: str = "", limit: int = 50) -> list[dict[str, Any]]:
        return self._manager.operations.list(plugin_key=key, limit=limit)

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        return self._manager.operations.get(operation_id)

    def _mcp_health(self, server_names: list[str]) -> list[dict[str, Any]]:
        manager = self._manager._mcp_manager
        if manager is None or not hasattr(manager, "health_snapshot"):
            return [{"name": name, "state": "runtime_unavailable"} for name in server_names]
        servers = {
            str(item.get("name") or ""): item
            for item in manager.health_snapshot().get("servers", [])
        }
        return [dict(servers.get(name) or {"name": name, "state": "not_registered"}) for name in server_names]
