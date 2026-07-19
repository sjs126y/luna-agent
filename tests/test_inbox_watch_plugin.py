from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from luna_agent.config import Settings
from luna_agent.plugins import PluginManager, PluginStatus


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "inbox_watch"
PLUGIN_KEY = "automation/inbox-watch"


def _manager(tmp_path) -> PluginManager:
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[PLUGIN_ROOT],
        plugins_enabled=[PLUGIN_KEY],
        plugins_config={
            PLUGIN_KEY: {
                "root": "data/inbox",
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


def test_inbox_watch_registers_controlled_tool_resources(tmp_path):
    manager = _manager(tmp_path)
    plugin = manager.list_plugins()[0]

    assert plugin.status is PluginStatus.LOADED
    assert plugin.commands_registered == ["inbox-status"]
    assert plugin.active_registration.resources.tools == (
        "list_directory",
        "file_info",
        "artifact_from_file",
    )
    assert plugin.active_registration.resources.conversation is True
    manager.unload_plugin(plugin.key)


@pytest.mark.asyncio
async def test_inbox_watch_settles_materializes_and_submits_once(tmp_path):
    manager = _manager(tmp_path)
    plugin = manager.list_plugins()[0]
    module = plugin.module
    root = tmp_path / "inbox"
    path = root / "note.txt"
    storage = _Storage()
    tool = _Tool(root, path)
    conversation = _Conversation()
    ctx = SimpleNamespace(resources=SimpleNamespace(storage=storage, tool=tool, conversation=conversation))
    config = module.InboxWatchConfig.model_validate({
        "root": str(root),
        "settle_seconds": 0,
        "active": {"sessions": ["wechat:c1:u1"]},
    })
    watcher = module.InboxWatcher(ctx, config)

    assert await watcher.poll_once(now=0) == [str(path)]
    assert await watcher.poll_once(now=10) == []

    artifact_calls = [item for item in tool.calls if item[0] == "artifact_from_file"]
    assert len(artifact_calls) == 1
    assert artifact_calls[0][2] == "wechat:c1:u1"
    assert conversation.requests[0]["artifact_ids"] == ["art_note"]
    assert storage.values["inbox-state.json"]["processed"][str(path)]["baseline_only"] is False
    manager.unload_plugin(plugin.key)


@pytest.mark.asyncio
async def test_inbox_watch_ignores_symlink_entries_and_rejects_oversized_file(tmp_path):
    manager = _manager(tmp_path)
    module = manager.list_plugins()[0].module
    root = tmp_path / "inbox"
    path = root / "large.pdf"
    storage = _Storage()
    tool = _Tool(root, path, size=100, include_symlink=True)
    ctx = SimpleNamespace(resources=SimpleNamespace(storage=storage, tool=tool, conversation=_Conversation()))
    config = module.InboxWatchConfig.model_validate({
        "root": str(root),
        "settle_seconds": 0,
        "max_file_bytes": 10,
        "active": {"sessions": ["wechat:c1:u1"]},
    })
    watcher = module.InboxWatcher(ctx, config)

    assert await watcher.poll_once(now=0) == []
    assert str(path) in storage.values["inbox-state.json"]["failures"]
    assert all(item[0] != "artifact_from_file" for item in tool.calls)


@pytest.mark.asyncio
async def test_inbox_watch_stops_retrying_unchanged_file_after_limit(tmp_path):
    manager = _manager(tmp_path)
    module = manager.list_plugins()[0].module
    root = tmp_path / "inbox"
    path = root / "failed.txt"
    storage = _Storage()
    tool = _Tool(root, path)

    class FailedConversation:
        async def submit(self, **kwargs):
            raise RuntimeError("delivery unavailable")

    ctx = SimpleNamespace(
        resources=SimpleNamespace(
            storage=storage,
            tool=tool,
            conversation=FailedConversation(),
        )
    )
    config = module.InboxWatchConfig.model_validate({
        "root": str(root),
        "settle_seconds": 0,
        "max_submission_attempts": 2,
        "active": {"sessions": ["wechat:c1:u1"]},
    })
    watcher = module.InboxWatcher(ctx, config)

    assert await watcher.poll_once(now=0) == []
    assert await watcher.poll_once(now=1) == []
    assert await watcher.poll_once(now=2) == []

    artifact_calls = [item for item in tool.calls if item[0] == "artifact_from_file"]
    assert len(artifact_calls) == 2
    assert storage.values["inbox-state.json"]["failures"][str(path)]["attempts"] == 2
    manager.unload_plugin(PLUGIN_KEY)


class _Storage:
    def __init__(self):
        self.values = {}

    def read_json(self, path, *, default=None, schema_version=None):
        return self.values.get(str(path), default)

    def write_json_atomic(self, path, value):
        self.values[str(path)] = value


class _Tool:
    def __init__(self, root, path, *, size=4, include_symlink=False):
        self.root = Path(root)
        self.path = Path(path)
        self.size = size
        self.include_symlink = include_symlink
        self.calls = []

    async def call(self, name, arguments, *, session_key=""):
        self.calls.append((name, dict(arguments), session_key))
        if name == "list_directory":
            entries = [{"name": self.path.name, "type": "file"}]
            if self.include_symlink:
                entries.append({"name": "link.pdf", "type": "symlink"})
            content = {"path": str(self.root), "entries": entries}
            return SimpleNamespace(status="success", content=json.dumps(content), artifacts=[])
        if name == "file_info":
            content = {
                "path": str(self.path),
                "type": "file",
                "size_bytes": self.size,
                "modified_at": "2026-07-18T00:00:00+00:00",
            }
            return SimpleNamespace(status="success", content=json.dumps(content), artifacts=[])
        if name == "artifact_from_file":
            return SimpleNamespace(
                status="success",
                content="{}",
                error="",
                artifacts=[SimpleNamespace(artifact_id="art_note")],
            )
        raise AssertionError(name)


class _Conversation:
    def __init__(self):
        self.requests = []

    async def submit(self, **kwargs):
        self.requests.append(kwargs)
        return "accepted"
