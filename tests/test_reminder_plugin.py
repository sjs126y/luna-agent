from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from luna_agent.config import Settings
from luna_agent.plugins import PluginManager, PluginStatus


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "reminder"
PLUGIN_KEY = "automation/reminder"


def _manager(tmp_path) -> PluginManager:
    settings = Settings(
        plugin_worker_isolation=False,
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[PLUGIN_ROOT],
        plugins_enabled=[PLUGIN_KEY],
        plugins_config={
            PLUGIN_KEY: {
                "active": {"enabled": False, "sessions": ["wechat:c1:u1"]},
            }
        },
        mcp_enabled=False,
        memory_external_provider="none",
    )
    manager = PluginManager(
        settings,
        plugin_dirs=[PLUGIN_ROOT],
        state_path=tmp_path / "plugin-state.json",
        include_builtin=False,
    )
    manager.load_enabled()
    return manager


def test_reminder_registers_tools_commands_and_active_runner(tmp_path):
    manager = _manager(tmp_path)
    plugin = manager.list_plugins()[0]

    assert plugin.status is PluginStatus.LOADED
    assert set(plugin.tools_registered) == {"reminder_create", "reminder_list", "reminder_cancel"}
    assert set(plugin.commands_registered) == {"reminders", "remind-cancel"}
    assert plugin.active_registration.resources.conversation is True
    manager.unload_plugin(plugin.key)


@pytest.mark.asyncio
async def test_reminder_runner_delivers_due_reminder_once(tmp_path):
    manager = _manager(tmp_path)
    plugin = manager.list_plugins()[0]
    module = plugin.module
    storage = _Storage()
    repository = module.ReminderRepository(storage)
    due_at = datetime.now(UTC) - timedelta(seconds=1)
    reminder = await repository.create(
        session_key="wechat:c1:u1",
        text="submit assignment",
        due_at=due_at,
    )
    conversation = _Conversation()
    ctx = SimpleNamespace(resources=SimpleNamespace(conversation=conversation))
    config = module.ReminderConfig.model_validate({"active": {"sessions": ["wechat:c1:u1"]}})
    runner = module.ReminderRunner(ctx, config, repository)

    await runner.fire_due(now=datetime.now(UTC))
    await runner.fire_due(now=datetime.now(UTC))

    items = await repository.list(session_key="wechat:c1:u1", include_completed=True)
    assert items[0]["status"] == "completed"
    assert items[0]["reminder_id"] == reminder["reminder_id"]
    assert len(conversation.requests) == 1
    assert conversation.requests[0]["request_id"] == f"reminder:{reminder['reminder_id']}"
    manager.unload_plugin(plugin.key)


@pytest.mark.asyncio
async def test_reminder_repository_recovers_firing_and_supports_cancel(tmp_path):
    manager = _manager(tmp_path)
    module = manager.list_plugins()[0].module
    storage = _Storage()
    repository = module.ReminderRepository(storage)
    reminder = await repository.create(
        session_key="wechat:c1:u1",
        text="recover",
        due_at=datetime.now(UTC),
    )
    await repository.mark(reminder["reminder_id"], "firing")

    recovered = module.ReminderRepository(storage)
    items = await recovered.list(session_key="wechat:c1:u1")
    assert items[0]["status"] == "scheduled"

    cancelled = await recovered.cancel(reminder["reminder_id"], session_key="wechat:c1:u1")
    assert cancelled["status"] == "cancelled"
    assert await recovered.list(session_key="wechat:c1:u1") == []


class _Storage:
    def __init__(self):
        self.values = {}

    def read_json(self, path, *, default=None, schema_version=None):
        return self.values.get(str(path), default)

    def write_json_atomic(self, path, value):
        self.values[str(path)] = {
            "schema_version": value["schema_version"],
            "reminders": [dict(item) for item in value["reminders"]],
        }


class _Conversation:
    def __init__(self):
        self.requests = []

    async def submit(self, **kwargs):
        self.requests.append(kwargs)
        return "accepted"
