import asyncio
import zipfile
from pathlib import Path

import pytest

from luna_agent.config import Settings
from luna_agent.plugins import PluginManager
from luna_agent.plugins.runtime import CapabilityKind


def _source(root: Path, *, version: str, value: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.yaml").write_text(
        "\n".join((
            "key: user/installed-demo",
            "name: Installed Demo",
            f"version: {version}",
            "entrypoint: installed_demo:register",
            "provides: [tools]",
            "enabled_by_default: true",
        )),
        encoding="utf-8",
    )
    (root / "installed_demo.py").write_text(
        "\n".join((
            "from luna_agent.tools.entry import ToolEntry",
            f"async def value(): return {value!r}",
            "def register(ctx):",
            "    ctx.register.tool(ToolEntry(",
            "        name='installed_value', description='installed value',",
            "        schema={'type': 'object', 'properties': {}}, handler=value,",
            "    ))",
        )),
        encoding="utf-8",
    )
    return root


def _manager(tmp_path: Path) -> PluginManager:
    return PluginManager(
        Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[]),
        plugin_dirs=[],
        state_path=tmp_path / "plugin-state.json",
        include_builtin=False,
    )


@pytest.mark.asyncio
async def test_install_and_update_use_immutable_packages(tmp_path):
    manager = _manager(tmp_path)
    source = _source(tmp_path / "source", version="1.0.0", value="one")

    first = await manager.install_plugin_runtime(source)
    first_package = manager.install_store.active_path(first.key)
    old_lease = await manager.capability_store.acquire()
    old_route = old_lease.view().resolve(CapabilityKind.TOOL, "installed_value")
    old_entry = manager.capability_payload(old_route.binding_id)

    _source(source, version="2.0.0", value="two-updated")
    second = await manager.install_plugin_runtime(source)
    second_package = manager.install_store.active_path(second.key)
    new_route = manager.capability_store.current.view().resolve(
        CapabilityKind.TOOL,
        "installed_value",
    )

    assert first_package != second_package
    assert first_package.is_dir() and second_package.is_dir()
    assert await old_entry.handler() == "one"
    assert await manager.capability_payload(new_route.binding_id).handler() == "two-updated"
    assert len(manager.install_store.packages()[second.key]["versions"]) == 2

    await old_lease.release()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_rollback_imports_handler_from_selected_immutable_package(tmp_path):
    manager = _manager(tmp_path)
    source = _source(tmp_path / "source", version="1.0.0", value="one")

    first = await manager.install_plugin_runtime(source)
    first_digest = first.package_digest
    _source(source, version="2.0.0", value="two")
    await manager.install_plugin_runtime(source)

    rolled_back = await manager.rollback_plugin_runtime(first.key, first_digest)
    route = manager.capability_store.current.view().resolve(
        CapabilityKind.TOOL,
        "installed_value",
    )
    entry = manager.capability_payload(route.binding_id)

    assert rolled_back.manifest.version == "1.0.0"
    assert rolled_back.package_digest == first_digest
    assert await entry.handler() == "one"


@pytest.mark.asyncio
async def test_uninstall_waits_for_snapshot_lease_and_keeps_data_by_default(tmp_path):
    manager = _manager(tmp_path)
    source = _source(tmp_path / "source", version="1.0.0", value="one")
    plugin = await manager.install_plugin_runtime(source)
    package_path = manager.install_store.active_path(plugin.key)
    data_path = manager.installer.data_root / plugin.key.replace("/", "__")
    data_path.mkdir(parents=True)
    (data_path / "state.txt").write_text("keep", encoding="utf-8")
    lease = await manager.capability_store.acquire()

    await manager.uninstall_plugin_runtime(plugin.key)

    assert package_path.exists()
    assert manager.install_store.packages()[plugin.key]["status"] == "pending_removal"
    assert manager.capability_store.current.view().resolve(
        CapabilityKind.TOOL,
        "installed_value",
    ) is None

    await lease.release()
    await asyncio.sleep(0)

    assert not package_path.exists()
    assert plugin.key not in manager.install_store.packages()
    assert (data_path / "state.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_install_only_stays_disabled_after_manager_restart(tmp_path):
    manager = _manager(tmp_path)
    source = _source(tmp_path / "source", version="1.0.0", value="one")

    installed = await manager.install_plugin_runtime(source, enable=False)
    restarted = _manager(tmp_path)
    restarted.discover()
    discovered = restarted._plugins[installed.key]

    assert installed.enabled is False
    assert manager.capability_store.current.view().resolve(
        CapabilityKind.TOOL,
        "installed_value",
    ) is None
    assert discovered.enabled is False


@pytest.mark.asyncio
async def test_installed_package_shadows_its_local_development_source(tmp_path):
    source = _source(
        tmp_path / "plugins" / "installed-demo",
        version="1.0.0",
        value="one",
    )
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[source.parent],
    )
    installer = PluginManager(
        settings,
        plugin_dirs=[source.parent],
        state_path=tmp_path / "plugin-state.json",
        include_builtin=False,
    )
    installed = await installer.install_plugin_runtime(source)
    package_path = installer.install_store.active_path(installed.key)

    restarted = PluginManager(
        settings,
        plugin_dirs=[source.parent],
        state_path=tmp_path / "plugin-state.json",
        include_builtin=False,
    )
    restarted.discover()
    discovered = restarted._plugins[installed.key]

    assert discovered.manifest.source == "installed"
    assert discovered.manifest.path == package_path
    assert discovered.status.name == "DISCOVERED"
    assert discovered.error is None


@pytest.mark.asyncio
async def test_disabled_installed_package_still_shadows_local_source(tmp_path):
    source = _source(
        tmp_path / "plugins" / "installed-demo",
        version="1.0.0",
        value="one",
    )
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[source.parent],
    )
    installer = PluginManager(
        settings,
        plugin_dirs=[source.parent],
        state_path=tmp_path / "plugin-state.json",
        include_builtin=False,
    )
    installed = await installer.install_plugin_runtime(source, enable=False)

    restarted = PluginManager(
        settings,
        plugin_dirs=[source.parent],
        state_path=tmp_path / "plugin-state.json",
        include_builtin=False,
    )
    restarted.discover()
    discovered = restarted._plugins[installed.key]

    assert discovered.manifest.source == "installed"
    assert discovered.enabled is False
    assert discovered.status.name == "DISABLED"
    assert discovered.error is None


def test_installer_rejects_archive_path_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../escape.txt", "bad")
        output.writestr("plugin.yaml", "invalid")

    with pytest.raises(ValueError, match="escapes package root"):
        _manager(tmp_path).installer.prepare(archive)
