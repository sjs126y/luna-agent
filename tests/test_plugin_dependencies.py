from __future__ import annotations

from pathlib import Path

import pytest

from personal_agent.config import Settings
from personal_agent.plugins import PluginManager, PluginStatus


def _write_plugin(
    root: Path,
    *,
    key: str,
    version: str = "0.1.0",
    requires: str = "",
) -> Path:
    plugin = root / key.replace("/", "__")
    plugin.mkdir(parents=True)
    module = key.rsplit("/", 1)[-1].replace("-", "_")
    (plugin / "plugin.yaml").write_text(
        "\n".join([
            "schema_version: 1",
            "plugin_api: '>=1,<2'",
            f"key: {key}",
            f"name: {key}",
            f"version: '{version}'",
            f"entrypoint: {module}:register",
            "provides: [command]",
            requires,
            "",
        ]),
        encoding="utf-8",
    )
    (plugin / f"{module}.py").write_text(
        "from lumora_plugin_sdk import CommandEntry\n"
        "def register(ctx):\n"
        f"    ctx.register.command(CommandEntry('{module}', 'test', lambda: 'ok'))\n",
        encoding="utf-8",
    )
    return plugin


def _manager(tmp_path: Path, root: Path) -> PluginManager:
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[root],
        plugins_enabled=["a/consumer", "z/provider"],
        memory_external_provider="none",
        mcp_enabled=False,
    )
    return PluginManager(settings, plugin_dirs=[root], include_builtin=False)


def test_dependency_load_order_precedes_alphabetical_plugin_order(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    _write_plugin(root, key="z/provider", version="0.3.0")
    _write_plugin(
        root,
        key="a/consumer",
        requires="requires:\n  plugins:\n    - key: z/provider\n      version: '>=0.3,<0.4'",
    )
    manager = _manager(tmp_path, root)

    manager.discover()
    manager.load_enabled()

    assert manager._plugins["z/provider"].status is PluginStatus.LOADED
    assert manager._plugins["a/consumer"].status is PluginStatus.LOADED
    assert manager.queries.plugin_info("a/consumer")["dependency_report"]["ok"] is True


def test_missing_or_incompatible_dependency_blocks_activation(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    _write_plugin(
        root,
        key="a/consumer",
        requires="requires:\n  plugins:\n    - key: z/provider\n      version: '>=2'",
    )
    manager = _manager(tmp_path, root)

    manager.discover()
    plugin = manager.load_plugin("a/consumer")

    assert plugin.status is PluginStatus.BLOCKED
    assert "Missing plugin dependency" in plugin.error


def test_dependency_cycle_blocks_all_members(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    _write_plugin(root, key="a/consumer", requires="requires:\n  plugins: [z/provider]")
    _write_plugin(root, key="z/provider", requires="requires:\n  plugins: [a/consumer]")
    manager = _manager(tmp_path, root)

    manager.discover()
    manager.load_enabled()

    assert manager._plugins["a/consumer"].status is PluginStatus.BLOCKED
    assert manager._plugins["z/provider"].status is PluginStatus.BLOCKED
    assert "cycle" in manager._plugins["a/consumer"].error.lower()


@pytest.mark.asyncio
async def test_uninstall_refuses_enabled_dependents_unless_forced(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    provider = _write_plugin(sources, key="z/provider", version="0.3.0")
    consumer = _write_plugin(
        sources,
        key="a/consumer",
        requires="requires:\n  plugins:\n    - key: z/provider\n      version: '>=0.3'",
    )
    manager = _manager(tmp_path, tmp_path / "empty")

    await manager.install_plugin_runtime(provider)
    await manager.install_plugin_runtime(consumer)

    with pytest.raises(RuntimeError, match="enabled dependents"):
        await manager.uninstall_plugin_runtime("z/provider")

    await manager.uninstall_plugin_runtime("z/provider", force=True)
    assert manager._plugins["a/consumer"].enabled is False
