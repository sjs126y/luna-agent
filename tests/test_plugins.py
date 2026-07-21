"""Plugin manager behavior."""

from __future__ import annotations

import asyncio

import pytest

from luna_agent.config import Settings
from luna_agent.plugins.core.manager import PluginManager
from luna_agent.plugins.core.models import LoadedPlugin
from luna_agent.plugins.models import CommandEntry, PluginManifest, PluginStatus
from luna_agent.plugins.runtime import PluginRuntimeState, RuntimeBackend
from luna_agent.skills.registry import skill_registry
from luna_agent.tools.registry import tool_registry


def _inprocess_settings(**kwargs):
    return Settings(plugin_worker_isolation=False, **kwargs)


def _settings(tmp_path, plugins_dir):
    return _inprocess_settings(
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
        ("schema_version", 2, "schema_version"),
        ("requires_env", ["OK", ""], "requires_env"),
        ("provides", 123, "provides"),
        ("tags", ["valid", ""], "tags"),
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


def test_only_platform_plugins_can_be_deferred():
    data = {
        "key": "user/lazy-mcp",
        "name": "Lazy MCP",
        "version": "1.0.0",
        "kind": "mcp",
        "entrypoint": "lazy_mcp:register",
        "deferred": True,
    }

    with pytest.raises(ValueError, match="only supported for platform"):
        PluginManifest.from_mapping(data)


def test_loaded_plugin_facade_keeps_generation_state_in_owned_models():
    manifest = PluginManifest.from_mapping({
        "key": "user/state-model",
        "name": "State Model",
        "version": "1.0.0",
        "entrypoint": "state_model:register",
    })
    plugin = LoadedPlugin(key=manifest.key, manifest=manifest, enabled=True)

    plugin.runtime_state = PluginRuntimeState.PREPARING
    plugin.runtime_backend = RuntimeBackend.WORKER
    plugin.worker_state = "recovering"
    plugin.active_restart_count = 2
    plugin.tools_registered.append("state_tool")
    view = plugin.view()

    assert plugin.definition.enabled is True
    assert plugin.generation.runtime_state is PluginRuntimeState.PREPARING
    assert plugin.generation.worker_status.state == "recovering"
    assert plugin.generation.active_status.restart_count == 2
    assert view.runtime_backend is RuntimeBackend.WORKER
    assert view.registrations["tools"] == 1
    with pytest.raises(TypeError):
        view.worker["state"] = "mutated"


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
        _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[plugins_dir]),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
    )
    manager.discover()
    report = manager.queries.plugin_info("invalid/bad-manifest")

    assert report["status"] == "ERROR"
    assert report["manifest_valid"] is False
    assert "field 'key'" in report["manifest_error"]
    assert report["entrypoint_importable"] is False
    assert report["entrypoint_error"] == ""
    assert report["enabled"] is False
    assert any("修复插件 manifest" in hint for hint in report["diagnostic_hints"])


def test_plugin_context_exposes_only_scoped_config(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "configured"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
schema_version: 1
key: user/configured
name: Configured
version: 1.0.0
entrypoint: configured:register
kind: integration
tags: [example, configured]
enabled_by_default: true
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "configured.py").write_text(
        """
from pydantic import BaseModel

class Config(BaseModel):
    timeout: int = 10

def register(ctx):
    parsed = ctx.parse_config(Config)
    assert parsed.timeout == 42
    assert ctx.config["timeout"] == 42
    try:
        ctx.config["timeout"] = 1
    except TypeError:
        pass
    else:
        raise AssertionError("plugin config must be read-only")
""".strip(),
        encoding="utf-8",
    )
    settings = _inprocess_settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[plugins_dir],
        plugins_config={
            "user/configured": {"timeout": 42},
            "user/other": {"private": True},
        },
    )
    manager = PluginManager(
        settings,
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )

    manager.load_enabled()
    report = manager.queries.plugin_info("user/configured")

    assert report["status"] == "LOADED"
    assert report["schema_version"] == 1
    assert report["kind"] == "integration"
    assert report["tags"] == ["example", "configured"]
    assert report["source"] == "local"


def test_non_object_manifest_is_preserved_for_doctor(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "List Manifest"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text("- not-an-object\n", encoding="utf-8")

    manager = PluginManager(
        _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[plugins_dir]),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
    )
    manager.discover()
    report = manager.queries.plugin_info("invalid/list-manifest")

    assert report["manifest_valid"] is False
    assert "must be an object" in report["manifest_error"]
    assert report["registered_items"]["tools"] == []


def test_validate_plugin_path_loads_package_plugin(tmp_path):
    plugin_dir = tmp_path / "plugins" / "pkg_example"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: user/pkg-example
name: Package Example
version: 1.0.0
entrypoint: pkg_example:register
enabled_by_default: false
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        """
from luna_agent.plugins.models import CommandEntry

def hello(args="", **kwargs):
    return "hello " + (args or "world")

def register(ctx):
    ctx.register.command(CommandEntry(
        name="pkghello",
        description="package command",
        handler=hello,
        scope="both",
    ))
""".strip(),
        encoding="utf-8",
    )

    manager = PluginManager(
        _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[]),
        plugin_dirs=[plugin_dir],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )

    report = manager.validate_plugin_path(plugin_dir)

    assert report["validation_ok"] is True
    assert report["validation_loaded"] is True
    assert report["registered_items"]["commands"] == ["pkghello"]
    assert manager.get_command("pkghello", scope="cli") is not None


def test_plugin_doctor_reports_deferred_reason_and_hint(tmp_path):
    settings = _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
    manager = PluginManager(settings, plugin_dirs=[], state_path=tmp_path / "state.json")
    manager.discover()

    report = manager.queries.plugin_info("platforms/telegram")

    assert report["status"] == "DEFERRED"
    assert report["entrypoint_checked"] is False
    assert report["deferred_reason"] == "平台插件会在网关解析平台适配器时加载"
    assert "平台插件会在网关解析平台适配器时加载" in report["diagnostic_hints"]


def test_deferred_plugin_doctor_does_not_import_before_load(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "lazy"
    marker = tmp_path / "imported.txt"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: user/lazy
name: Lazy Plugin
version: 1.0.0
kind: platform
entrypoint: lazy_plugin:register
enabled_by_default: true
deferred: true
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "lazy_plugin.py").write_text(
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("imported", encoding="utf-8")

def register(ctx):
    pass
""".strip(),
        encoding="utf-8",
    )

    manager = PluginManager(
        _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[plugins_dir]),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )
    manager.discover()
    report = manager.queries.plugin_info("user/lazy")

    assert report["status"] == "DEFERRED"
    assert report["entrypoint_checked"] is False
    assert not marker.exists()


def test_user_plugin_source_is_forced_by_scan_boundary(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "pretend_builtin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: user/pretend-builtin
name: Pretend Builtin
version: 1.0.0
entrypoint: pretend_plugin:register
source: builtin
enabled_by_default: true
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "pretend_plugin.py").write_text("def register(ctx): pass\n", encoding="utf-8")

    manager = PluginManager(
        _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[plugins_dir]),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )
    manager.discover()
    report = manager.queries.plugin_info("user/pretend-builtin")

    assert report["source"] == "local"
    assert report["declared_source"] == "builtin"
    assert report["source_boundary"] == "local"
    assert any("source=builtin" in item for item in report["boundary_warnings"])
    assert report["manifest_path"].endswith("plugin.yaml")


def test_user_plugin_cannot_use_reserved_builtin_key(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "reserved"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: builtin/pretend
name: Reserved Builtin Key
version: 1.0.0
entrypoint: reserved_plugin:register
enabled_by_default: true
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "reserved_plugin.py").write_text("def register(ctx): pass\n", encoding="utf-8")

    manager = PluginManager(
        _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[plugins_dir]),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )

    manager.discover()
    report = manager.queries.plugin_info("builtin/pretend")

    assert report["status"] == "ERROR"
    assert report["enabled"] is False
    assert "reserved builtin key" in report["error"]
    assert any("builtin/*" in item for item in report["boundary_warnings"])


def test_installed_plugin_declared_local_source_is_not_boundary_warning(tmp_path):
    installed_root = tmp_path / "data" / "plugins" / "installed-local"
    installed_root.mkdir(parents=True)
    (installed_root / "plugin.yaml").write_text(
        """
key: user/installed-local
name: Installed Local Declaration
version: 1.0.0
entrypoint: installed_local:register
source: local
enabled_by_default: true
""".strip(),
        encoding="utf-8",
    )
    (installed_root / "installed_local.py").write_text(
        "def register(ctx): pass\n",
        encoding="utf-8",
    )

    manager = PluginManager(
        _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[installed_root]),
        plugin_dirs=[installed_root],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )
    manager.discover()
    report = manager.queries.plugin_info("user/installed-local")

    assert report["source"] == "installed"
    assert report["declared_source"] == "local"
    assert report["source_boundary"] == "installed"
    assert not any("声明 source" in item for item in report["boundary_warnings"])


def test_plugin_doctor_reports_manifest_warnings(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "platformish"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: user/platformish
name: Platformish
version: 1.0.0
kind: platform
entrypoint: platformish:register
requires_env:
  - lower_case_env
typo_field: ignored
enabled_by_default: true
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "platformish.py").write_text("def register(ctx): pass\n", encoding="utf-8")

    manager = PluginManager(
        _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[plugins_dir]),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )

    manager.discover()
    report = manager.queries.plugin_info("user/platformish", check_entrypoint=False)

    assert report["manifest_unknown_fields"] == ["typo_field"]
    assert any("provides 包含 platform" in item for item in report["manifest_warnings"])
    assert any("deferred: true" in item for item in report["manifest_warnings"])
    assert any("大写环境变量名" in item for item in report["manifest_warnings"])
    assert report["status"] == "DISCOVERED"


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
from luna_agent.plugins.models import CommandEntry
from luna_agent.tools.entry import ToolEntry

async def sample_tool():
    return "tool-ok"

async def sample_command(args="", **kwargs):
    return "command-ok:" + args

async def sample_hook(value):
    return value + "-hook"

def register(ctx):
    ctx.register.tool(ToolEntry(
        name="plugin_sample_tool",
        description="sample",
        schema={"type": "object", "properties": {}},
        handler=sample_tool,
    ))
    ctx.register.hook("sample_hook", sample_hook, priority=10)
    ctx.register.command(CommandEntry(
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


def test_missing_env_plugin_can_load_for_setup_only(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_SUCH_ENV_FOR_PLUGIN_SETUP_TEST", raising=False)
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "setup-only"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: user/setup-only
name: Setup Only
version: 1.0.0
entrypoint: setup_plugin
requires_env: [NO_SUCH_ENV_FOR_PLUGIN_SETUP_TEST]
enabled_by_default: true
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "setup_plugin.py").write_text("", encoding="utf-8")

    manager = PluginManager(
        _settings(tmp_path, plugins_dir),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
    )
    manager.discover()
    plugin = manager.load_plugin("user/setup-only", allow_missing_env=True)

    try:
        assert plugin.status == PluginStatus.LOADED
    finally:
        manager.unload_plugin("user/setup-only")


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
        _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[plugins_dir]),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
    )
    manager.discover()
    plugin = manager.load_plugin("user/broken")
    report = manager.queries.plugin_info(plugin.key)

    assert report["status"] == "ERROR"
    assert report["entrypoint_importable"] is True
    assert "RuntimeError: doctor boom" in report["error"]
    assert "RuntimeError: doctor boom" in report["error_traceback"]
    assert report["registered_items"]["tools"] == []


def test_plugin_registration_failure_rolls_back_all_contributions(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "partial"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: user/partial
name: Partial Plugin
version: 1.0.0
entrypoint: partial_plugin:register
enabled_by_default: true
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "partial_plugin.py").write_text(
        """
from luna_agent.plugins.models import CommandEntry
from luna_agent.tools.entry import ToolEntry

async def handler(**kwargs):
    return "ok"

def register(ctx):
    ctx.register.tool(ToolEntry(
        name="partial_registration_tool",
        description="partial",
        schema={"type": "object", "properties": {}},
        handler=handler,
    ))
    ctx.register.hook("partial_hook", handler)
    ctx.register.command(CommandEntry(
        name="partial-command",
        description="partial",
        handler=handler,
    ))
    raise RuntimeError("stop registration")
""".strip(),
        encoding="utf-8",
    )
    manager = PluginManager(
        _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[plugins_dir]),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )

    manager.load_enabled()
    plugin = manager._plugins["user/partial"]

    assert plugin.status == PluginStatus.ERROR
    assert tool_registry.get("partial_registration_tool") is None
    assert manager.get_command("partial-command") is None
    assert "partial_hook" not in manager.hooks
    assert all(count == 0 for count in plugin.registration_counts().values())


def test_plugin_registration_rejects_cross_plugin_tool_conflict(tmp_path):
    plugins_dir = tmp_path / "plugins"
    for key, module in (("first", "first_plugin"), ("second", "second_plugin")):
        plugin_dir = plugins_dir / key
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.yaml").write_text(
            f"""
key: user/{key}
name: {key.title()} Plugin
version: 1.0.0
entrypoint: {module}:register
enabled_by_default: true
""".strip(),
            encoding="utf-8",
        )
        (plugin_dir / f"{module}.py").write_text(
            f"""
from luna_agent.tools.entry import ToolEntry

async def handler(**kwargs):
    return "{key}"

def register(ctx):
    ctx.register.tool(ToolEntry(
        name="shared_plugin_tool",
        description="{key}",
        schema={{"type": "object", "properties": {{}}}},
        handler=handler,
    ))
""".strip(),
            encoding="utf-8",
        )
    manager = PluginManager(
        _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[plugins_dir]),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )

    manager.load_enabled()

    first = manager._plugins["user/first"]
    second = manager._plugins["user/second"]
    assert first.status == PluginStatus.LOADED
    assert second.status == PluginStatus.ERROR
    assert "already registered by plugin 'user/first'" in (second.error or "")
    assert tool_registry.get("shared_plugin_tool") is not None
    assert tool_registry.get("shared_plugin_tool").description == "first"
    manager.disable_plugin("user/first")


def test_plugin_registers_flat_and_nested_skill_bundles(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "skill_bundle"
    nested_dir = plugin_dir / "skills" / "review-pr"
    nested_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: user/skill-bundle
name: Skill Bundle
version: 1.0.0
entrypoint: skill_bundle:register
provides: [skills]
enabled_by_default: true
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "skill_bundle.py").write_text(
        "def register(ctx): ctx.register.skills('skills')\n",
        encoding="utf-8",
    )
    (plugin_dir / "skills" / "flat.md").write_text(
        "# Flat skill\n\nFlat body.",
        encoding="utf-8",
    )
    (nested_dir / "SKILL.md").write_text(
        """
---
name: review-pr
description: Review a pull request safely
triggers: [/review-pr]
---

# Review PR

Inspect the diff before suggesting changes.
""".strip(),
        encoding="utf-8",
    )
    manager = PluginManager(
        _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[plugins_dir]),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )

    manager.load_enabled()
    plugin = manager._plugins["user/skill-bundle"]

    assert plugin.status == PluginStatus.LOADED
    assert plugin.skills_registered == ["flat", "review-pr"]
    review = skill_registry.get("review-pr")
    assert review is not None
    assert review.plugin_key == "user/skill-bundle"
    assert review.triggers == ["/review-pr"]
    assert "Inspect the diff" in (skill_registry.load("review-pr") or "")
    manager.disable_plugin("user/skill-bundle")
    assert skill_registry.get("review-pr") is None


def test_plugin_skill_bundle_cannot_escape_plugin_root(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "escaping_skill"
    plugin_dir.mkdir(parents=True)
    (plugins_dir / "outside").mkdir()
    (plugins_dir / "outside" / "secret.md").write_text("# Secret", encoding="utf-8")
    (plugin_dir / "plugin.yaml").write_text(
        """
key: user/escaping-skill
name: Escaping Skill
version: 1.0.0
entrypoint: escaping_skill:register
enabled_by_default: true
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "escaping_skill.py").write_text(
        "def register(ctx): ctx.register.skills('../outside')\n",
        encoding="utf-8",
    )
    manager = PluginManager(
        _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[plugins_dir]),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )

    manager.load_enabled()
    plugin = manager._plugins["user/escaping-skill"]

    assert plugin.status == PluginStatus.ERROR
    assert "escapes package root" in (plugin.error or "")
    assert skill_registry.get("secret") is None


def test_plugin_registers_mcp_config_bundle(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "mcp_bundle"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: user/mcp-bundle
name: MCP Bundle
version: 1.0.0
entrypoint: mcp_bundle:register
provides: [mcp]
enabled_by_default: true
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "mcp_bundle.py").write_text(
        "def register(ctx): ctx.register.mcp('mcp.yaml')\n",
        encoding="utf-8",
    )
    (plugin_dir / "mcp.yaml").write_text(
        """
servers:
  - name: local-demo
    transport: stdio
    command: python
    args: [-m, demo]
  - name: remote-demo
    transport: streamable_http
    url: https://example.com/mcp
    headers_env:
      Authorization: REMOTE_DEMO_TOKEN
""".strip(),
        encoding="utf-8",
    )
    manager = PluginManager(
        _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[plugins_dir]),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )

    manager.load_enabled()
    plugin = manager._plugins["user/mcp-bundle"]
    configs = manager.get_mcp_servers()

    assert plugin.status == PluginStatus.LOADED
    assert plugin.mcp_servers_registered == ["local-demo", "remote-demo"]
    assert [config.name for config in configs] == ["local-demo", "remote-demo"]
    assert manager.mcp_server_registry.revision == 2
    manager.disable_plugin("user/mcp-bundle")
    assert manager.get_mcp_servers() == []


def test_plugin_mcp_bundle_conflict_rolls_back_all_servers(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "bad_mcp"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: user/bad-mcp
name: Bad MCP
version: 1.0.0
entrypoint: bad_mcp:register
enabled_by_default: true
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "bad_mcp.py").write_text(
        "def register(ctx): ctx.register.mcp('mcp.json')\n",
        encoding="utf-8",
    )
    (plugin_dir / "mcp.json").write_text(
        """
{
  "servers": [
    {"name": "duplicate", "command": "python"},
    {"name": "duplicate", "command": "other"}
  ]
}
""".strip(),
        encoding="utf-8",
    )
    manager = PluginManager(
        _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[plugins_dir]),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )

    manager.load_enabled()
    plugin = manager._plugins["user/bad-mcp"]

    assert plugin.status == PluginStatus.ERROR
    assert "already registered by this plugin" in (plugin.error or "")
    assert manager.get_mcp_servers() == []
    assert plugin.mcp_servers_registered == []


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


def test_discover_is_idempotent_for_same_manifest_path(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "sample"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: user/sample
name: Sample
version: 1.0.0
entrypoint: sample_plugin:register
enabled_by_default: true
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "sample_plugin.py").write_text("def register(ctx): pass\n", encoding="utf-8")

    manager = PluginManager(
        _settings(tmp_path, plugins_dir),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )

    manager.discover()
    manager.discover()

    assert manager._plugins["user/sample"].status == PluginStatus.DISCOVERED
    assert manager._plugins["user/sample"].error is None


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
    settings = _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
    manager = PluginManager(settings, plugin_dirs=[], state_path=tmp_path / "state.json")
    manager.discover()

    skills = manager._plugins["builtin/skills"]
    assert skills.manifest.source == "builtin"
    assert skills.manifest.entrypoint == "luna_agent.plugins.builtin.skills.builtin:register"
    assert skills.manifest.path is not None
    assert "src/luna_agent/plugins/builtin/skills/builtin" in skills.manifest.path.as_posix()

    tools = manager._plugins["builtin/tools"]
    assert tools.manifest.entrypoint == "luna_agent.plugins.builtin.tools.builtin:register"
    assert tools.manifest.path is not None
    assert "src/luna_agent/plugins/builtin/tools/builtin" in tools.manifest.path.as_posix()

    telegram = manager._plugins["platforms/telegram"]
    assert telegram.status == PluginStatus.DEFERRED
    assert telegram.manifest.entrypoint == "luna_agent.plugins.builtin.platforms.telegram:register"
    assert telegram.manifest.path is not None
    assert "src/luna_agent/plugins/builtin/platforms/telegram" in telegram.manifest.path.as_posix()

    qq = manager._plugins["platforms/qq"]
    assert qq.status == PluginStatus.DEFERRED
    assert qq.manifest.entrypoint == "luna_agent.plugins.builtin.platforms.qq:register"
    assert qq.manifest.path is not None
    assert "src/luna_agent/plugins/builtin/platforms/qq" in qq.manifest.path.as_posix()

    memory_luna = manager._plugins["memory/luna"]
    assert memory_luna.manifest.entrypoint == "luna_agent.plugins.builtin.memory.luna:register"
    assert memory_luna.manifest.path is not None
    assert "src/luna_agent/plugins/builtin/memory/luna" in memory_luna.manifest.path.as_posix()

    memory_mem0 = manager._plugins["memory/mem0"]
    assert memory_mem0.manifest.entrypoint == "luna_agent.plugins.builtin.memory.mem0:register"
    assert memory_mem0.manifest.path is not None
    assert "src/luna_agent/plugins/builtin/memory/mem0" in memory_mem0.manifest.path.as_posix()

    from luna_agent.plugins.builtin.skills.builtin import register as skills_register

    assert callable(skills_register)


def test_builtin_tools_use_explicit_plugin_registration(tmp_path):
    settings = _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
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


def test_builtin_tool_delegate_setup_uses_agent_runtime_settings(tmp_path, monkeypatch):
    from luna_agent.plugins.builtin.tools.builtin import _setup_delegate

    captured = {}

    def fake_setup_delegate(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        "luna_agent.plugins.builtin.tools.builtin.delegate.setup_delegate",
        fake_setup_delegate,
    )
    settings = _inprocess_settings(
        agent_data_dir=tmp_path / "data",
        agent_runtime_max_tokens=1234,
        agent_runtime_max_concurrent_runs=2,
        agent_runtime_max_tool_calls=3,
        agent_runtime_history_limit=4,
    )

    _setup_delegate(
        call_fn=lambda **kwargs: None,
        tools=[],
        max_tokens=9999,
        settings=settings,
    )

    assert captured["max_tokens"] == 1234
    assert captured["max_concurrent_runs"] == 2
    assert captured["max_tool_calls"] == 3
    assert captured["history_limit"] == 4
    assert captured["run_store_path"] == tmp_path / "data" / "agent_runs.jsonl"


def test_builtin_skills_use_explicit_plugin_registration(tmp_path):
    settings = _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
    manager = PluginManager(settings, plugin_dirs=[], state_path=tmp_path / "state.json")
    manager.discover()

    plugin = manager.load_plugin("builtin/skills")
    assert plugin.status == PluginStatus.LOADED
    assert "python-expert" in plugin.skills_registered


def test_disable_memory_provider_plugin_unregisters_provider(tmp_path):
    from luna_agent.memory.provider_registry import memory_provider_registry

    memory_provider_registry.clear()
    settings = _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
    manager = PluginManager(settings, plugin_dirs=[], state_path=tmp_path / "state.json")
    manager.discover()

    manager.load_plugin("memory/luna")
    assert memory_provider_registry.get("luna") is not None

    manager.disable_plugin("memory/luna")
    assert memory_provider_registry.get("luna") is None


def test_memory_provider_doctor_reports_registered_provider(tmp_path):
    from luna_agent.memory.provider_registry import memory_provider_registry

    memory_provider_registry.clear()
    settings = _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
    manager = PluginManager(settings, plugin_dirs=[], state_path=tmp_path / "state.json")
    manager.discover()
    manager.load_plugin("memory/mem0")

    report = manager.queries.plugin_info("memory/mem0")
    assert report["status"] == "LOADED"
    assert report["registered"]["memory_providers"] == 1
    assert report["registered_items"]["memory_providers"] == ["mem0"]
    memory_provider_registry.clear()


def test_wechat_plugin_registers_platform_setup(tmp_path):
    from luna_agent.platforms.core import platform_registry

    settings = _inprocess_settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
    manager = PluginManager(settings, plugin_dirs=[], state_path=tmp_path / "state.json")
    manager.discover()

    plugin = manager.load_plugin("platforms/wechat")

    assert plugin.status == PluginStatus.LOADED
    entry = platform_registry.get("wechat")
    assert entry is not None
    assert entry.setup_fn is not None
    manager.unload_plugin("platforms/wechat")


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


@pytest.mark.asyncio
async def test_typed_hook_registration_uses_shared_hook_manager(tmp_path):
    from luna_agent.hooks import HookEnvelope, HookEvent, HookScope, PreToolUseOutcome

    manager = PluginManager(
        _settings(tmp_path, tmp_path / "plugins"),
        plugin_dirs=[],
        state_path=tmp_path / "state.json",
    )

    async def protect(event):
        return PreToolUseOutcome.block("protected")

    manager.register_event_hook(
        "plugin/demo",
        HookEvent.PRE_TOOL_USE,
        protect,
        matcher="^write$",
    )

    outcome = await manager.hook_manager.dispatch(HookEnvelope(
        event_name=HookEvent.PRE_TOOL_USE,
        scope=HookScope.TURN,
        payload={"tool_name": "write"},
    ))

    assert outcome.blocked is True
    assert outcome.reason == "protected"
    manager.hook_manager.unregister_owner("plugin/demo")


def test_plugin_rejects_removed_runtime_hook_names(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "legacy-runtime-hook"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: user/legacy-runtime-hook
name: Legacy Runtime Hook
version: 1.0.0
entrypoint: legacy_runtime_hook:register
enabled_by_default: true
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "legacy_runtime_hook.py").write_text(
        """
async def before_tool(*args, **kwargs):
    return None

def register(ctx):
    ctx.register.hook("on_before_tool_exec", before_tool)
""".strip(),
        encoding="utf-8",
    )
    manager = PluginManager(
        _settings(tmp_path, plugins_dir),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )

    manager.load_enabled()

    plugin = manager._plugins["user/legacy-runtime-hook"]
    assert plugin.status == PluginStatus.ERROR
    assert "register a typed HookEvent" in (plugin.error or "")


def test_plugin_typed_hook_is_removed_when_plugin_is_disabled(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "typed-hook"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: user/typed-hook
name: Typed Hook
version: 1.0.0
entrypoint: typed_hook:register
enabled_by_default: true
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "typed_hook.py").write_text(
        """
from luna_agent.hooks import HookEvent, PreToolUseOutcome

async def protect(event):
    return PreToolUseOutcome.block("protected")

def register(ctx):
    ctx.register.hook(
        HookEvent.PRE_TOOL_USE,
        protect,
        name="protect",
        matcher="^write$",
        priority=10,
    )
""".strip(),
        encoding="utf-8",
    )
    manager = PluginManager(
        _settings(tmp_path, plugins_dir),
        plugin_dirs=[plugins_dir],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )

    manager.load_enabled()
    plugin = manager._plugins["user/typed-hook"]

    assert plugin.status == PluginStatus.LOADED
    assert plugin.hooks_registered == ["PreToolUse:protect:10"]
    assert len(manager.hook_manager.registrations()) == 1

    manager.disable_plugin("user/typed-hook")

    assert manager.hook_manager.registrations() == []
