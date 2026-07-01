"""Plugin manager behavior."""

from __future__ import annotations

import asyncio

import pytest

from personal_agent.config import Settings
from personal_agent.plugins.manager import PluginManager
from personal_agent.plugins.models import CommandEntry, PluginStatus
from personal_agent.tools.registry import tool_registry


def _settings(tmp_path, plugins_dir):
    return Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[plugins_dir],
        plugins_enabled=["user/sample", "user/protected", "user/missing-env"],
        plugins_disabled=[],
    )


def test_plugin_load_registers_and_unloads_entries(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "sample"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: user/sample
name: Sample Plugin
version: 1.0.0
entrypoint: sample_plugin:register
enabled_by_default: false
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "sample_plugin.py").write_text(
        """
from personal_agent.plugins.models import CommandEntry
from personal_agent.tools.entry import ToolEntry

async def sample_tool():
    return "tool-ok"

async def sample_command(args="", **kwargs):
    return "command-ok:" + args

async def sample_hook(value):
    return value + "-hook"

def register(ctx):
    ctx.register_tool(ToolEntry(
        name="plugin_sample_tool",
        description="sample",
        schema={"type": "object", "properties": {}},
        handler=sample_tool,
    ))
    ctx.register_hook("sample_hook", sample_hook, priority=10)
    ctx.register_command(CommandEntry(
        name="hello",
        description="sample command",
        handler=sample_command,
        scope="slash",
    ))
""".strip(),
        encoding="utf-8",
    )

    manager = PluginManager(
        _settings(tmp_path, plugins_dir),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
    )
    manager.discover()
    plugin = manager.load_plugin("user/sample")

    assert plugin.status == PluginStatus.LOADED
    assert "plugin_sample_tool" in plugin.tools_registered
    assert tool_registry.get("plugin_sample_tool") is not None
    assert manager.get_command("hello") is not None

    result = asyncio.run(manager.invoke_hook("sample_hook", "value"))
    assert result == "value-hook"

    manager.disable_plugin("user/sample")
    assert tool_registry.get("plugin_sample_tool") is None
    assert manager.get_command("hello") is None


def test_missing_env_plugin_enters_error(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_SUCH_ENV_FOR_PLUGIN_TEST", raising=False)
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "missing"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: user/missing-env
name: Missing Env
version: 1.0.0
entrypoint: missing_plugin
requires_env: [NO_SUCH_ENV_FOR_PLUGIN_TEST]
enabled_by_default: true
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "missing_plugin.py").write_text("", encoding="utf-8")

    manager = PluginManager(
        _settings(tmp_path, plugins_dir),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
    )
    manager.discover()
    plugin = manager.load_plugin("user/missing-env")

    assert plugin.status == PluginStatus.ERROR
    assert "Missing required env" in (plugin.error or "")


def test_plugin_doctor_reports_traceback_and_registered_items(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "broken"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: user/broken
name: Broken Plugin
version: 1.0.0
entrypoint: broken_plugin:register
enabled_by_default: true
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "broken_plugin.py").write_text(
        """
def register(ctx):
    raise RuntimeError("doctor boom")
""".strip(),
        encoding="utf-8",
    )

    manager = PluginManager(
        Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[plugins_dir]),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
    )
    manager.discover()
    plugin = manager.load_plugin("user/broken")
    report = manager.doctor_plugin(plugin.key)

    assert report["status"] == "ERROR"
    assert report["entrypoint_importable"] is True
    assert "RuntimeError: doctor boom" in report["error"]
    assert "RuntimeError: doctor boom" in report["error_traceback"]
    assert report["registered_items"]["tools"] == []


def test_duplicate_plugin_key_marks_existing_error(tmp_path):
    plugins_dir = tmp_path / "plugins"
    for name in ("one", "two"):
        plugin_dir = plugins_dir / name
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.yaml").write_text(
            """
key: user/sample
name: Duplicate
version: 1.0.0
entrypoint: duplicate_plugin
enabled_by_default: true
""".strip(),
            encoding="utf-8",
        )
        (plugin_dir / "duplicate_plugin.py").write_text("", encoding="utf-8")

    manager = PluginManager(
        _settings(tmp_path, plugins_dir),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
    )
    manager.discover()

    assert manager._plugins["user/sample"].status == PluginStatus.ERROR
    assert "Duplicate plugin key" in (manager._plugins["user/sample"].error or "")


def test_command_cannot_override_core_command(tmp_path):
    manager = PluginManager(
        _settings(tmp_path, tmp_path / "plugins"),
        plugin_dirs=[],
        state_path=tmp_path / "state.json",
    )
    with pytest.raises(ValueError):
        manager.register_command(CommandEntry(
            name="stop",
            description="bad",
            handler=lambda **kwargs: "bad",
            scope="slash",
            plugin_key="user/bad",
        ))


def test_builtin_manifests_are_discovered_from_project_plugins(tmp_path):
    settings = Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
    manager = PluginManager(settings, plugin_dirs=[], state_path=tmp_path / "state.json")
    manager.discover()

    skills = manager._plugins["builtin/skills"]
    assert skills.manifest.source == "builtin"
    assert skills.manifest.entrypoint == "personal_agent.plugins.builtin.skills.builtin:register"
    assert skills.manifest.path is not None
    assert "src/personal_agent/plugins/builtin/skills/builtin" in skills.manifest.path.as_posix()

    tools = manager._plugins["builtin/tools"]
    assert tools.manifest.entrypoint == "personal_agent.plugins.builtin.tools.builtin:register"
    assert tools.manifest.path is not None
    assert "src/personal_agent/plugins/builtin/tools/builtin" in tools.manifest.path.as_posix()

    telegram = manager._plugins["platforms/telegram"]
    assert telegram.status == PluginStatus.DEFERRED
    assert telegram.manifest.entrypoint == "personal_agent.plugins.builtin.platforms.telegram:register"
    assert telegram.manifest.path is not None
    assert "src/personal_agent/plugins/builtin/platforms/telegram" in telegram.manifest.path.as_posix()

    from personal_agent.plugins.builtin.skills.builtin import register as skills_register

    assert callable(skills_register)


def test_builtin_tools_use_explicit_plugin_registration(tmp_path):
    settings = Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
    manager = PluginManager(settings, plugin_dirs=[], state_path=tmp_path / "state.json")
    manager.discover()

    plugin = manager.load_plugin("builtin/tools")
    assert plugin.status == PluginStatus.LOADED
    assert "calculator" in plugin.tools_registered
    assert "read" in plugin.tools_registered
    assert tool_registry.get("calculator") is not None

    manager.disable_plugin("builtin/tools")
    assert tool_registry.get("calculator") is None

    manager.enable_plugin("builtin/tools")
    plugin = manager.load_plugin("builtin/tools")
    assert plugin.status == PluginStatus.LOADED
    assert tool_registry.get("calculator") is not None


def test_builtin_skills_use_explicit_plugin_registration(tmp_path):
    settings = Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
    manager = PluginManager(settings, plugin_dirs=[], state_path=tmp_path / "state.json")
    manager.discover()

    plugin = manager.load_plugin("builtin/skills")
    assert plugin.status == PluginStatus.LOADED
    assert "python-expert" in plugin.skills_registered


@pytest.mark.asyncio
async def test_hook_priority_and_fail_open(tmp_path):
    manager = PluginManager(
        _settings(tmp_path, tmp_path / "plugins"),
        plugin_dirs=[],
        state_path=tmp_path / "state.json",
    )
    order = []

    async def first(value):
        order.append("first")
        return "first-result"

    async def fail(value):
        order.append("fail")
        raise RuntimeError("boom")

    async def second(value):
        order.append("second")
        return "second-result"

    manager.register_hook("a", "hook", second, priority=30)
    manager.register_hook("b", "hook", first, priority=10)
    manager.register_hook("c", "hook", fail, priority=20)

    assert await manager.invoke_hook("hook", "input") == "second-result"
    assert order == ["first", "fail", "second"]
