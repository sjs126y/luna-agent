"""Read-only inspection ports for the live application runtime."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


class RuntimeInspectionPort:
    """Narrow routing port; domain managers remain owners of their state."""

    def __init__(self, runtime) -> None:
        self._runtime = runtime

    async def runtime_summary(self) -> dict[str, Any]:
        health = self._runtime.health_snapshot()
        memory = await self._runtime.memory_manager.health_snapshot()
        plugin_runtime = dict(health.get("plugin_runtime") or {})
        runtime_counts = dict(plugin_runtime.get("runtime_counts") or {})
        gateway = dict(health.get("gateway") or {})
        mcp = dict(health.get("mcp") or {})
        coordinator = dict(health.get("coordinator") or {})
        warnings: list[str] = []
        if not health.get("core_ready"):
            warnings.append("core_not_ready")
        if health.get("boot_ok") is False:
            warnings.append("boot_failed")
        if mcp.get("failed_count"):
            warnings.append("mcp_degraded")
        worker_supervisor = dict(plugin_runtime.get("worker_supervisor") or {})
        if worker_supervisor.get("recovery_task_count"):
            warnings.append("plugins_degraded")
        if plugin_runtime.get("degraded_mcp_count"):
            warnings.append("plugin_mcp_degraded")
        status = "failed" if not health.get("core_ready") else "degraded" if warnings else "healthy"
        return {
            "ok": status != "failed",
            "schema_version": 2,
            "captured_at": datetime.now(UTC).isoformat(),
            "source": "live",
            "status": status,
            "runtime": {
                "core_ready": bool(health.get("core_ready")),
                "closed": bool(health.get("closed")),
                "gateway_running": bool(health.get("gateway_running")),
                "mcp": {
                    "enabled": bool(mcp.get("enabled")),
                    "running": bool(mcp.get("running")),
                    "connected_count": int(mcp.get("connected_count") or 0),
                    "failed_count": int(mcp.get("failed_count") or 0),
                },
                "plugins": {
                    "count": int(health.get("plugins") or 0),
                    "runtime": {
                        "worker_count": int(worker_supervisor.get("worker_count") or 0),
                        "running_count": int(worker_supervisor.get("running_count") or 0),
                        "recovery_task_count": int(worker_supervisor.get("recovery_task_count") or 0),
                        "environment_lease_count": int(worker_supervisor.get("environment_lease_count") or 0),
                        # Compatibility aliases retained for existing TUI consumers.
                        "active_workers": int(worker_supervisor.get("running_count") or 0),
                        "unhealthy_workers": int(
                            runtime_counts.get("failed", 0)
                            + runtime_counts.get("circuit_open", 0)
                        ),
                        "degraded_mcp_plugins": list(plugin_runtime.get("degraded_mcp_plugins") or []),
                    },
                },
                "conversation": {
                    "active": int(coordinator.get("active_count") or 0),
                    "queued": int(coordinator.get("queued_count") or 0),
                },
                "delivery": {
                    "platforms": len(gateway.get("platforms") or []),
                    "connected": sum(1 for item in gateway.get("platforms") or [] if item.get("connected")),
                },
                "memory": {
                    "provider": memory.get("effective_provider", ""),
                    "pending_migration": int((memory.get("migration") or {}).get("global_pending", 0)),
                    "pending_index": int((memory.get("index") or {}).get("global_pending", 0)),
                },
            },
            "warnings": warnings,
            "refs": [
                "plugin_inspect", "conversation_inspect", "platform_inspect",
                "config_inspect", "memory_inspect", "audit_inspect", "logs_query",
            ],
        }

    def conversation(self):
        return self._runtime.conversation_service.queries

    def conversation_runtime_snapshot(self) -> dict[str, Any]:
        return self._runtime.conversation_coordinator.snapshot()

    def plugin(self):
        return self._runtime.plugin_manager.queries

    def gateway(self):
        return self._runtime.gateway

    def settings(self):
        return self._runtime.settings

    def memory(self):
        return self._runtime.memory_manager

    def audit_path(self):
        from luna_agent.tools.audit import audit_path
        return audit_path()

    def logs_path(self):
        return self._runtime.data_dir / "logs" / "agent.log"
