from types import SimpleNamespace

import pytest

from luna_agent.runtime_inspection import RuntimeInspectionPort


@pytest.mark.asyncio
async def test_runtime_summary_projects_worker_and_plugin_mcp_health():
    health = {
        "core_ready": True,
        "closed": False,
        "gateway_running": True,
        "boot_ok": True,
        "mcp": {
            "enabled": True,
            "running": True,
            "connected_count": 2,
            "failed_count": 0,
        },
        "plugin_runtime": {
            "runtime_counts": {"loaded": 2},
            "worker_supervisor": {
                "worker_count": 3,
                "running_count": 2,
                "recovery_task_count": 1,
                "environment_lease_count": 3,
            },
            "degraded_mcp_count": 1,
            "degraded_mcp_plugins": [{
                "plugin_key": "integrations/codex-bridge",
                "servers": [{"name": "codex", "state": "reconnecting"}],
            }],
        },
        "plugins": 4,
        "gateway": {"platforms": []},
        "coordinator": {"active_count": 0, "queued_count": 0},
    }
    runtime = SimpleNamespace(
        health_snapshot=lambda: health,
        memory_manager=SimpleNamespace(health_snapshot=_memory_health),
    )

    result = await RuntimeInspectionPort(runtime).runtime_summary()

    assert result["schema_version"] == 2
    assert result["status"] == "degraded"
    assert result["runtime"]["plugins"]["runtime"] == {
        "worker_count": 3,
        "running_count": 2,
        "recovery_task_count": 1,
        "environment_lease_count": 3,
        "active_workers": 2,
        "unhealthy_workers": 0,
        "degraded_mcp_plugins": health["plugin_runtime"]["degraded_mcp_plugins"],
    }
    assert "plugin_mcp_degraded" in result["warnings"]
    assert "plugins_degraded" in result["warnings"]


@pytest.mark.asyncio
async def test_runtime_summary_degrades_on_memory_maintenance_failure():
    health = {
        "core_ready": True,
        "closed": False,
        "gateway_running": True,
        "boot_ok": True,
        "mcp": {"enabled": True, "running": True, "connected_count": 1, "failed_count": 0},
        "plugin_runtime": {"worker_supervisor": {}, "runtime_counts": {}},
        "plugins": 1,
        "gateway": {"platforms": []},
        "coordinator": {"active_count": 0, "queued_count": 0},
    }

    async def memory_health_with_failure():
        return {
            "effective_provider": "luna",
            "migration": {"global_pending": 15},
            "index": {"global_pending": 129},
            "maintenance": {
                "migration": {"failed": 4},
                "index": {"failed": 0},
            },
        }

    runtime = SimpleNamespace(
        health_snapshot=lambda: health,
        memory_manager=SimpleNamespace(health_snapshot=memory_health_with_failure),
    )

    result = await RuntimeInspectionPort(runtime).runtime_summary()

    assert result["status"] == "degraded"
    assert "memory_maintenance_failed" in result["warnings"]
    assert result["runtime"]["memory"]["maintenance_failed"] == 4


async def _memory_health():
    return {
        "effective_provider": "luna",
        "migration": {"global_pending": 0},
        "index": {"global_pending": 0},
    }
