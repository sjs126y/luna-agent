from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from luna_agent.config import Settings
from luna_agent.plugins import PluginManager, PluginStatus


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "examples" / "plugins" / "workspace_watch"
PLUGIN_KEY = "integrations/workspace-watch"


def _manager(tmp_path, config: dict) -> PluginManager:
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[PLUGIN_ROOT],
        plugins_enabled=[PLUGIN_KEY],
        plugins_config={PLUGIN_KEY: config},
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


def test_workspace_watch_registers_active_runner_and_status_command(tmp_path):
    manager = _manager(tmp_path, {
        "paths": ["TODO.md"],
        "session_key": "wechat:c1:u1",
        "active": {
            "enabled": False,
            "sessions": ["wechat:c1:u1"],
        },
    })
    plugin = next(item for item in manager.list_plugins() if item.key == PLUGIN_KEY)

    assert plugin.status is PluginStatus.LOADED
    assert plugin.active_enabled is False
    assert plugin.active_runner.root_task is None
    assert plugin.active_registration.resources.tools == ("file_info",)
    assert plugin.active_registration.resources.conversation is True
    assert manager.get_command("workspace-watch-status", scope="cli") is not None
    assert not (tmp_path / "data" / "plugins" / "data" / "integrations__workspace-watch").exists()


@pytest.mark.asyncio
async def test_workspace_watch_can_be_uninstalled_and_reinstalled(tmp_path):
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[],
        plugins_config={PLUGIN_KEY: {"active": {"enabled": False}}},
        mcp_enabled=False,
        memory_external_provider="none",
    )
    manager = PluginManager(
        settings,
        plugin_dirs=[],
        state_path=tmp_path / "plugin-state.json",
        include_builtin=False,
    )

    first = await manager.install_plugin_runtime(PLUGIN_ROOT)
    first_path = Path(first.manifest.path)
    assert first.status is PluginStatus.LOADED
    assert first.manifest.source == "installed"
    assert first_path.is_dir()

    removed = await manager.uninstall_plugin_runtime(PLUGIN_KEY)
    await asyncio.sleep(0)
    assert removed.runtime_state.value != "active"
    assert PLUGIN_KEY not in manager.install_store.packages()
    assert not first_path.exists()

    second = await manager.install_plugin_runtime(PLUGIN_ROOT)
    assert second.status is PluginStatus.LOADED
    assert second.runtime_instance_id != first.runtime_instance_id
    assert Path(second.manifest.path).is_dir()


@pytest.mark.asyncio
async def test_workspace_watch_baselines_settles_and_notifies_once(tmp_path):
    manager = _manager(tmp_path, {
        "paths": ["TODO.md"],
        "session_key": "wechat:c1:u1",
        "settle_seconds": 10,
        "active": {
            "enabled": False,
            "sessions": ["wechat:c1:u1"],
        },
    })
    plugin = next(item for item in manager.list_plugins() if item.key == PLUGIN_KEY)
    module = plugin.module
    storage = _Storage()
    conversation = _Conversation()
    tool = _Tool([
        _file_info("2026-07-18T00:00:00+00:00", 10),
        _file_info("2026-07-18T00:01:00+00:00", 20),
        _file_info("2026-07-18T00:01:00+00:00", 20),
        _file_info("2026-07-18T00:01:00+00:00", 20),
    ])
    ctx = SimpleNamespace(
        resources=SimpleNamespace(
            storage=storage,
            tool=tool,
            conversation=conversation,
        ),
    )
    config = module.WorkspaceWatchConfig.model_validate({
        "paths": ["TODO.md"],
        "session_key": "wechat:c1:u1",
        "settle_seconds": 10,
    })
    watcher = module.WorkspaceWatcher(ctx, config)

    assert await watcher.poll_once(now=0) == []
    assert await watcher.poll_once(now=5) == []
    assert await watcher.poll_once(now=15) == ["TODO.md"]
    assert await watcher.poll_once(now=30) == []

    assert len(conversation.requests) == 1
    assert conversation.requests[0]["session_key"] == "wechat:c1:u1"
    assert "TODO.md" in conversation.requests[0]["text"]
    persisted = json.loads(storage.values["signatures.json"])
    assert "TODO.md" in persisted


def _file_info(modified_at: str, size: int) -> dict:
    return {
        "path": str(Path.cwd() / "TODO.md"),
        "type": "file",
        "size_bytes": size,
        "modified_at": modified_at,
    }


class _Storage:
    def __init__(self) -> None:
        self.values = {}

    def read_text(self, path, *, default=""):
        return self.values.get(str(path), default)

    def write_text(self, path, text):
        self.values[str(path)] = str(text)


class _Tool:
    def __init__(self, payloads):
        self.payloads = list(payloads)

    async def call(self, name, arguments):
        assert name == "file_info"
        assert arguments == {"path": "TODO.md"}
        payload = self.payloads.pop(0)
        return SimpleNamespace(status="success", content=json.dumps(payload))


class _Conversation:
    def __init__(self) -> None:
        self.requests = []

    async def submit(self, **kwargs):
        self.requests.append(kwargs)
        return "accepted"
