"""Plugin manager behavior."""

from __future__ import annotations

import asyncio

import pytest

from personal_agent.config import Settings
from personal_agent.plugins.manager import PluginManager
from personal_agent.plugins.models import CommandEntry, PluginManifest, PluginStatus
from personal_agent.tools.registry import tool_registry


def _settings(tmp_path, plugins_dir):
    return Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[plugins_dir],
        plugins_enabled=["user/sample", "user/protected", "user/missing-env"],
        plugins_disabled=[],
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("key", "User/Sample", "field 'key'"),
        ("entrypoint", "sample-plugin:register", "field 'entrypoint'"),
        ("kind", "unknown", "field 'kind'"),
        ("source", "remote", "field 'source'"),
        ("requires_env", ["OK", ""], "requires_env"),
        ("provides", 123, "provides"),
        ("enabled_by_default", "true", "enabled_by_default"),
        ("deferred", 1, "deferred"),
        ("record_import_delta", "yes", "record_import_delta"),
    ],
)
def test_plugin_manifest_schema_rejects_invalid_values(field, value, message):
    data = {
        "key": "user/sample",
        "name": "Sample",
        "version": "1.0.0",
        "entrypoint": "sample_plugin:register",
    }
    data[field] = value

    with pytest.raises(ValueError, match=message):
        PluginManifest.from_mapping(data)


def test_invalid_manifest_is_preserved_for_doctor(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "Bad Manifest"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: User/Bad
name: Bad Manifest
version: 1.0.0
entrypoint: bad_plugin:register
enabled_by_default: true
""".strip(),
        encoding="utf-8",
    )

    manager = PluginManager(
        Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[plugins_dir]),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
    )
    manager.discover()
    report = manager.doctor_plugin("invalid/bad-manifest")

    assert report["status"] == "ERROR"
    assert report["manifest_valid"] is False
    assert "field 'key'" in report["manifest_error"]
    assert report["entrypoint_importable"] is False
    assert report["entrypoint_error"] == ""
    assert report["enabled"] is False
    assert any("修复插件 manifest" in hint for hint in report["diagnostic_hints"])


def test_non_object_manifest_is_preserved_for_doctor(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "List Manifest"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text("- not-an-object\n", encoding="utf-8")

    manager = PluginManager(
        Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[plugins_dir]),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
    )
    manager.discover()
    report = manager.doctor_plugin("invalid/list-manifest")

    assert report["manifest_valid"] is False
    assert "must be an object" in report["manifest_error"]
    assert report["registered_items"]["tools"] == []


def test_plugin_doctor_reports_deferred_reason_and_hint(tmp_path):
    settings = Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
    manager = PluginManager(settings, plugin_dirs=[], state_path=tmp_path / "state.json")
    manager.discover()

    report = manager.doctor_plugin("platforms/telegram")

    assert report["status"] == "DEFERRED"
    assert report["deferred_reason"] == "平台插件会在网关解析平台适配器时加载"
    assert "平台插件会在网关解析平台适配器时加载" in report["diagnostic_hints"]


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

    memory_file = manager._plugins["memory/file"]
    assert memory_file.manifest.entrypoint == "personal_agent.plugins.builtin.memory.file:register"
    assert memory_file.manifest.path is not None
    assert "src/personal_agent/plugins/builtin/memory/file" in memory_file.manifest.path.as_posix()

    memory_embedding = manager._plugins["memory/embedding"]
    assert memory_embedding.manifest.entrypoint == "personal_agent.plugins.builtin.memory.embedding:register"
    assert memory_embedding.manifest.path is not None
    assert "src/personal_agent/plugins/builtin/memory/embedding" in memory_embedding.manifest.path.as_posix()

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


def test_disable_memory_provider_plugin_removes_hooks(tmp_path):
    settings = Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
    manager = PluginManager(settings, plugin_dirs=[], state_path=tmp_path / "state.json")
    manager.discover()

    manager.load_plugin("memory/file")
    assert "on_session_selected" in manager.hooks
    assert "create_builtin_memory_provider" in manager.hooks

    manager.disable_plugin("memory/file")
    assert "on_session_selected" not in manager.hooks
    assert "create_builtin_memory_provider" not in manager.hooks


@pytest.mark.asyncio
async def test_builtin_memory_provider_is_created_by_hook(tmp_path):
    settings = Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
    manager = PluginManager(settings, plugin_dirs=[], state_path=tmp_path / "state.json")
    manager.discover()
    manager.load_plugin("memory/file")

    provider = await manager.invoke_hook(
        "create_builtin_memory_provider",
        system_dir=tmp_path / "system",
    )

    assert provider is not None
    assert provider.__class__.__name__ == "FileMemoryProvider"


@pytest.mark.asyncio
async def test_external_memory_provider_is_created_by_hook_and_removed_on_disable(tmp_path):
    settings = Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
    manager = PluginManager(settings, plugin_dirs=[], state_path=tmp_path / "state.json")
    manager.discover()
    manager.load_plugin("memory/embedding")

    provider = await manager.invoke_hook(
        "create_external_memory_provider",
        data_dir=tmp_path / "memory",
        force=True,
    )

    assert provider is not None
    assert provider.__class__.__name__ == "EmbeddingMemoryProvider"
    assert "create_external_memory_provider" in manager.hooks

    manager.disable_plugin("memory/embedding")
    assert "create_external_memory_provider" not in manager.hooks
    assert await manager.invoke_hook(
        "create_external_memory_provider",
        data_dir=tmp_path / "memory",
        force=True,
    ) is None


def test_memory_provider_doctor_reports_registered_hooks(tmp_path):
    settings = Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
    manager = PluginManager(settings, plugin_dirs=[], state_path=tmp_path / "state.json")
    manager.discover()
    manager.load_plugin("memory/file")
    manager.load_plugin("memory/embedding")

    file_report = manager.doctor_plugin("memory/file")
    assert file_report["status"] == "LOADED"
    assert file_report["registered"]["hooks"] == 3
    assert file_report["registered_items"]["hooks"] == [
        "configure:10",
        "on_session_selected:10",
        "create_builtin_memory_provider:10",
    ]

    embedding_report = manager.doctor_plugin("memory/embedding")
    assert embedding_report["status"] == "LOADED"
    assert embedding_report["registered"]["hooks"] == 1
    assert embedding_report["registered_items"]["hooks"] == [
        "create_external_memory_provider:10",
    ]


def test_wechat_plugin_registers_login_hook(tmp_path):
    settings = Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
    manager = PluginManager(settings, plugin_dirs=[], state_path=tmp_path / "state.json")
    manager.discover()

    plugin = manager.load_plugin("platforms/wechat")

    assert plugin.status == PluginStatus.LOADED
    assert "wechat_qr_login:10" in plugin.hooks_registered
    assert "wechat_qr_login" in manager.hooks


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
