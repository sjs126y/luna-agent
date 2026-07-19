from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from luna_agent.config import Settings
from luna_agent.plugins import PluginManager
from luna_agent.plugins.builtin.tools.builtin.plugin_tools import (
    _build_resources,
    _manage_resources,
    plugin_build,
    plugin_build_entry,
    plugin_inspect,
    plugin_inspect_entry,
    plugin_manage,
    plugin_manage_entry,
)
from luna_agent.tools.registry import dispatch_tool_search
from luna_agent.tools.runtime_context import reset_current_tool_agent, set_current_tool_agent
from luna_agent.tools.sandbox import get_sandbox, init_sandbox


def _plugin_source(root: Path, *, version: str = "1.0.0") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.yaml").write_text(
        "\n".join((
            "schema_version: 1",
            "key: user/agent-tool-demo",
            "name: Agent Tool Demo",
            f"version: {version}",
            'plugin_api: ">=1,<2"',
            "entrypoint: agent_tool_demo:register",
            "enabled_by_default: true",
        )),
        encoding="utf-8",
    )
    (root / "agent_tool_demo.py").write_text(
        "def register(ctx):\n    pass\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def plugin_sandbox(tmp_path):
    previous = get_sandbox()
    init_sandbox([tmp_path], ["**/.env", "**/.git/**"])
    try:
        yield tmp_path
    finally:
        init_sandbox(previous.roots, previous.blocked, read_roots=previous.read_roots)


def _manager(tmp_path: Path) -> PluginManager:
    return PluginManager(
        Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[]),
        plugin_dirs=[],
        state_path=tmp_path / "plugin-state.json",
        include_builtin=False,
    )


def _decode(value) -> dict:
    assert isinstance(value, str), getattr(value, "text", value)
    return json.loads(value)


@pytest.mark.asyncio
async def test_plugin_tools_are_discoverable_but_not_core() -> None:
    result = json.loads(await dispatch_tool_search("plugin install package inspect"))
    names = {item["name"] for item in result["hits"]}

    assert {"plugin_inspect", "plugin_build", "plugin_manage"} <= names
    assert plugin_inspect_entry.toolset == "plugin"
    assert plugin_build_entry.approval_mode == "prompt"
    assert plugin_manage_entry.approval_mode == "prompt"
    assert plugin_manage_entry.is_destructive is True


@pytest.mark.asyncio
async def test_plugin_build_validates_tests_and_packages(plugin_sandbox: Path) -> None:
    source = _plugin_source(plugin_sandbox / "source")
    output = plugin_sandbox / "dist" / "demo.zip"

    validation = _decode(await plugin_build("validate", str(source)))
    contract = _decode(await plugin_build("test", str(source)))
    packaged = _decode(await plugin_build("package", str(source), str(output)))

    assert validation["ok"] is True
    assert validation["plugin_key"] == "user/agent-tool-demo"
    assert contract["ok"] is True
    assert packaged["ok"] is True
    assert packaged["path"] == str(output)
    assert len(packaged["sha256"]) == 64
    assert output.is_file()


def test_plugin_tool_resources_cover_source_and_package_output(plugin_sandbox: Path) -> None:
    source = _plugin_source(plugin_sandbox / "source")
    output = plugin_sandbox / "dist" / "demo.zip"

    build = _build_resources({
        "action": "package",
        "source": str(source),
        "output": str(output),
    })
    install = _manage_resources({"action": "install", "source": str(output)})

    assert [(item.access, item.resource) for item in build] == [
        ("read", str(source)),
        ("write", str(output)),
    ]
    assert [(item.access, item.resource) for item in install] == [
        ("read", str(output)),
    ]


@pytest.mark.asyncio
async def test_plugin_manage_uses_live_manager_and_preserves_data(plugin_sandbox: Path) -> None:
    manager = _manager(plugin_sandbox)
    source = _plugin_source(plugin_sandbox / "source")
    token = set_current_tool_agent(SimpleNamespace(_plugin_manager=manager))
    try:
        installed = _decode(await plugin_manage("install", source=str(source)))
        inspected = _decode(await plugin_inspect("info", plugin_key="user/agent-tool-demo"))
        data_path = manager.installer.data_root / "user__agent-tool-demo"
        data_path.mkdir(parents=True)
        (data_path / "state.json").write_text("{}", encoding="utf-8")
        disabled = _decode(await plugin_manage("disable", plugin_key="user/agent-tool-demo"))
        enabled = _decode(await plugin_manage("enable", plugin_key="user/agent-tool-demo"))
        uninstalled = _decode(await plugin_manage("uninstall", plugin_key="user/agent-tool-demo"))
    finally:
        reset_current_tool_agent(token)

    assert installed["plugin_key"] == "user/agent-tool-demo"
    assert installed["effective_next_turn"] is True
    assert inspected["plugin"]["version"] == "1.0.0"
    assert disabled["status"] == "DISABLED"
    assert enabled["status"] == "LOADED"
    assert uninstalled["plugin_key"] == "user/agent-tool-demo"
    assert (data_path / "state.json").is_file()


def test_plugin_package_rejects_symlinks(plugin_sandbox: Path) -> None:
    source = _plugin_source(plugin_sandbox / "source")
    outside = plugin_sandbox / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (source / "linked.txt").symlink_to(outside)

    from luna_agent.plugins.devtools import package_plugin

    with pytest.raises(ValueError, match="symbolic link"):
        package_plugin(source, plugin_sandbox / "package.zip")
