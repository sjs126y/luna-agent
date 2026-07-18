from __future__ import annotations

from pathlib import Path

import pytest

from personal_agent.config import Settings
from personal_agent.plugins.control_state import PluginControlStateStore
from personal_agent.plugins.core.manager import PluginManager
from personal_agent.plugins.events import PluginEventJournal
from personal_agent.plugins.operations import PluginOperationTracker


@pytest.mark.asyncio
async def test_operation_tracker_records_stages_events_and_failure(tmp_path):
    store = PluginControlStateStore(tmp_path / "control.json")
    events = PluginEventJournal(store)
    tracker = PluginOperationTracker(store, events)

    async with tracker.track("user/demo", "reload") as operation:
        operation.stage("publishing")

    completed = tracker.get(operation.operation_id)
    assert completed["status"] == "completed"
    assert completed["stage"] == "completed"
    assert [item["event"] for item in events.list("user/demo")] == [
        "operation_completed",
        "operation_started",
    ]

    with pytest.raises(RuntimeError, match="broken"):
        async with tracker.track("user/demo", "reload"):
            raise RuntimeError("broken")

    assert tracker.list(plugin_key="user/demo", limit=1)[0]["status"] == "failed"


def test_control_store_marks_running_operations_interrupted_on_restart(tmp_path):
    path = tmp_path / "control.json"
    store = PluginControlStateStore(path)
    store.put_operation({
        "operation_id": "pop_old",
        "plugin_key": "user/demo",
        "action": "install",
        "stage": "preparing",
        "status": "running",
    })

    restarted = PluginControlStateStore(path)

    assert restarted.operations()[0]["status"] == "interrupted"


@pytest.mark.asyncio
async def test_plugin_queries_include_versions_and_latest_operation(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "plugin.yaml").write_text(
        "\n".join((
            "schema_version: 1",
            "key: user/managed",
            "name: Managed",
            "version: 1.0.0",
            "entrypoint: managed:register",
        )),
        encoding="utf-8",
    )
    (source / "managed.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
    manager = PluginManager(
        Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[]),
        plugin_dirs=[],
        include_builtin=False,
    )

    plugin = await manager.install_plugin_runtime(source)
    report = manager.queries.plugin_info(plugin.key)

    assert report["management_schema_version"] == 1
    assert report["latest_operation"]["action"] == "install"
    assert report["installed_versions"][0]["active"] is True
    assert report["latest_event"]["event"] == "operation_completed"
