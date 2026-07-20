import asyncio
from pathlib import Path
import sys

import pytest

from luna_agent.config import Settings
from luna_agent.plugins import PluginManager, PluginStatus
from luna_agent.plugins.runtime import CapabilityKind, PluginRuntimeState
from luna_agent.platforms.core import platform_registry
from luna_agent.tools.entry import ToolEntry
from luna_agent.tools.registry import tool_registry


def _write_plugin(root: Path, result: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.yaml").write_text(
        "\n".join((
            "key: user/hot-reload",
            "name: Hot Reload",
            "version: 1.0.0",
            "entrypoint: hot_reload:register",
            "provides: [tools]",
            "enabled_by_default: true",
        )),
        encoding="utf-8",
    )
    (root / "hot_reload.py").write_text(
        "\n".join((
            "from luna_agent.tools.entry import ToolEntry",
            "",
            "async def handler():",
            f"    return {result!r}",
            "",
            "def register(ctx):",
            "    ctx.register.tool(ToolEntry(",
            "        name='hot_value',",
            "        description='Return the hot reload value',",
            "        schema={'type': 'object', 'properties': {}},",
            "        handler=handler,",
            "    ))",
        )),
        encoding="utf-8",
    )


def _write_package_plugin(root: Path, result: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    package = root / "isolated_plugin"
    package.mkdir(exist_ok=True)
    (root / "plugin.yaml").write_text(
        "\n".join((
            "key: user/isolated-hot-reload",
            "name: Isolated Hot Reload",
            "version: 1.0.0",
            "entrypoint: isolated_plugin:register",
            "provides: [tools]",
            "enabled_by_default: true",
        )),
        encoding="utf-8",
    )
    (package / "value.py").write_text(f"VALUE = {result!r}\n", encoding="utf-8")
    (package / "__init__.py").write_text(
        "\n".join((
            "from luna_agent.tools.entry import ToolEntry",
            "from .value import VALUE",
            "",
            "async def handler():",
            "    return VALUE",
            "",
            "def register(ctx):",
            "    ctx.register.tool(ToolEntry(",
            "        name='isolated_hot_value',",
            "        description='Return the isolated generation value',",
            "        schema={'type': 'object', 'properties': {}},",
            "        handler=handler,",
            "    ))",
        )),
        encoding="utf-8",
    )


def _write_boot_scoped_plugin(
    root: Path,
    result: str,
    *,
    deferred: bool = False,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    manifest_lines = [
        "key: user/boot-scoped",
        "name: Boot Scoped",
        "version: 1.0.0",
        "entrypoint: boot_scoped:register",
        "provides: [tools, platforms]",
        "enabled_by_default: true",
    ]
    if deferred:
        manifest_lines.extend(("kind: platform", "deferred: true"))
    (root / "plugin.yaml").write_text(
        "\n".join(manifest_lines),
        encoding="utf-8",
    )
    (root / "boot_scoped.py").write_text(
        "\n".join((
            "from luna_agent.platforms.core import PlatformEntry",
            "from luna_agent.tools.entry import ToolEntry",
            "",
            f"VALUE = {result!r}",
            "",
            "async def handler():",
            "    return VALUE",
            "",
            "def factory(*_args, **_kwargs):",
            "    return VALUE",
            "",
            "def register(ctx):",
            "    ctx.register.tool(ToolEntry(",
            "        name='boot_scoped_value',",
            "        description='Return the generation value',",
            "        schema={'type': 'object', 'properties': {}},",
            "        handler=handler,",
            "    ))",
            "    ctx.register.platform(PlatformEntry(",
            "        name='boot-scoped-platform',",
            "        factory=factory,",
            "        check_fn=lambda _config: True,",
            "    ))",
        )),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_reload_publishes_new_routes_and_drains_old_runtime(tmp_path):
    plugin_dir = tmp_path / "plugins" / "hot_reload"
    _write_plugin(plugin_dir, "version-one")
    manager = PluginManager(
        Settings(
            agent_data_dir=tmp_path / "data",
            plugins_dirs=[plugin_dir.parent],
            plugin_worker_isolation=False,
        ),
        plugin_dirs=[plugin_dir.parent],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )
    first = manager.load_plugin("user/hot-reload")
    assert first.status is PluginStatus.LOADED

    class Coordinator:
        async def submit(self, request):
            return request

    manager.bind_application_ports(
        conversation_coordinator=Coordinator(),
        delivery_service=object(),
    )
    old_context = first.ctx

    old_lease = await manager.capability_store.acquire()
    old_route = old_lease.view().resolve(CapabilityKind.TOOL, "hot_value")
    old_entry = manager.capability_payload(old_route.binding_id)

    _write_plugin(plugin_dir, "version-two-with-new-code")
    second = manager.reload_plugin("user/hot-reload")
    new_route = manager.capability_store.current.view().resolve(
        CapabilityKind.TOOL,
        "hot_value",
    )
    new_entry = manager.capability_payload(new_route.binding_id)

    assert second.status is PluginStatus.LOADED
    assert second.runtime_instance_id != first.runtime_instance_id
    assert second.generation_id != first.generation_id
    assert await old_entry.handler() == "version-one"
    assert await new_entry.handler() == "version-two-with-new-code"
    assert first.runtime_state is PluginRuntimeState.DRAINING
    with pytest.raises(RuntimeError, match="not active"):
        await old_context.conversation.submit(session_key="test", text="must not escape")

    await old_lease.release()
    await asyncio.sleep(0)

    assert first.runtime_state is PluginRuntimeState.STOPPED
    assert manager.capability_payload(old_route.binding_id) is None


@pytest.mark.asyncio
async def test_package_generations_keep_relative_imports_isolated_until_lease_release(tmp_path):
    plugin_dir = tmp_path / "plugins" / "isolated"
    _write_package_plugin(plugin_dir, "version-one")
    manager = PluginManager(
        Settings(
            agent_data_dir=tmp_path / "data",
            plugins_dirs=[plugin_dir.parent],
            plugin_worker_isolation=False,
        ),
        plugin_dirs=[plugin_dir.parent],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )
    first = manager.load_plugin("user/isolated-hot-reload")
    old_namespace = first.module_namespace
    old_lease = await manager.capability_store.acquire()
    old_route = old_lease.view().resolve(CapabilityKind.TOOL, "isolated_hot_value")
    old_entry = manager.capability_payload(old_route.binding_id)

    _write_package_plugin(plugin_dir, "version-two")
    second = manager.reload_plugin("user/isolated-hot-reload")
    new_route = manager.capability_store.current.view().resolve(
        CapabilityKind.TOOL,
        "isolated_hot_value",
    )
    new_entry = manager.capability_payload(new_route.binding_id)

    assert first.module.__name__.startswith(f"{old_namespace}.")
    assert second.module.__name__.startswith(f"{second.module_namespace}.")
    assert old_namespace != second.module_namespace
    assert any(name.startswith(f"{old_namespace}.") for name in sys.modules)
    assert any(name.startswith(f"{second.module_namespace}.") for name in sys.modules)
    assert await old_entry.handler() == "version-one"
    assert await new_entry.handler() == "version-two"

    await old_lease.release()
    await asyncio.sleep(0)

    assert not any(name == old_namespace or name.startswith(f"{old_namespace}.") for name in sys.modules)
    assert any(
        name == second.module_namespace or name.startswith(f"{second.module_namespace}.")
        for name in sys.modules
    )


@pytest.mark.asyncio
async def test_staged_generation_does_not_replace_compatibility_registry(tmp_path):
    plugin_dir = tmp_path / "plugins" / "hot_reload"
    _write_plugin(plugin_dir, "version-one")
    manager = PluginManager(
        Settings(
            agent_data_dir=tmp_path / "data",
            plugins_dirs=[plugin_dir.parent],
            plugin_worker_isolation=False,
        ),
        plugin_dirs=[plugin_dir.parent],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )
    first = manager.load_plugin("user/hot-reload")
    old_entry = tool_registry.get("hot_value")
    old_revision = manager.capability_store.current.revision

    _write_plugin(plugin_dir, "version-two")
    candidate = manager._activate_manifest(
        first.manifest,
        previous=first,
        evict=True,
        publish=False,
    )

    assert candidate.status is PluginStatus.LOADED
    assert candidate.registration_transaction.committed is False
    assert tool_registry.get("hot_value") is old_entry
    assert manager.capability_store.current.revision == old_revision

    await manager._discard_staged_plugin(candidate, first)


@pytest.mark.asyncio
async def test_boot_scoped_capabilities_wait_for_restart_during_reload(tmp_path):
    plugin_dir = tmp_path / "plugins" / "boot-scoped"
    _write_boot_scoped_plugin(plugin_dir, "version-one")
    manager = PluginManager(
        Settings(
            agent_data_dir=tmp_path / "data",
            plugins_dirs=[plugin_dir.parent],
            plugin_worker_isolation=False,
        ),
        plugin_dirs=[plugin_dir.parent],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )
    first = manager.load_plugin("user/boot-scoped")
    old_platform = platform_registry.get("boot-scoped-platform")
    old_route = manager.capability_store.current.view().resolve(
        CapabilityKind.PLATFORM,
        "boot-scoped-platform",
    )
    manager.seal_boot_scope()

    _write_boot_scoped_plugin(plugin_dir, "version-two")
    second = await manager.reload_plugin_runtime("user/boot-scoped")
    tool_route = manager.capability_store.current.view().resolve(
        CapabilityKind.TOOL,
        "boot_scoped_value",
    )
    platform_route = manager.capability_store.current.view().resolve(
        CapabilityKind.PLATFORM,
        "boot-scoped-platform",
    )

    assert await manager.capability_payload(tool_route.binding_id).handler() == "version-two"
    assert platform_registry.get("boot-scoped-platform") is old_platform
    assert platform_route.runtime_instance_id == old_route.runtime_instance_id
    assert platform_route.runtime_instance_id == first.runtime_instance_id
    assert second.runtime_instance_id != first.runtime_instance_id
    assert manager.queries.plugin_info(second.key)["boot_scope"] == {
        "sealed": True,
        "pending_restart": True,
        "capabilities": ["platform"],
    }

    manager._boot_scope_sealed = False
    manager.unload_plugin(second.key)
    platform_registry.unregister("boot-scoped-platform")


def test_deferred_platform_can_complete_first_load_after_boot_scope_seal(tmp_path):
    plugin_dir = tmp_path / "plugins" / "boot-scoped"
    _write_boot_scoped_plugin(plugin_dir, "version-one", deferred=True)
    manager = PluginManager(
        Settings(
            agent_data_dir=tmp_path / "data",
            plugins_dirs=[plugin_dir.parent],
            plugin_worker_isolation=False,
        ),
        plugin_dirs=[plugin_dir.parent],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )
    manager.discover()
    manager.seal_boot_scope()

    try:
        loaded = manager.load_plugin("user/boot-scoped")
        route = manager.capability_store.current.view().resolve(
            CapabilityKind.PLATFORM,
            "boot-scoped-platform",
        )

        assert loaded.status is PluginStatus.LOADED
        assert platform_registry.get("boot-scoped-platform") is not None
        assert route.runtime_instance_id == loaded.runtime_instance_id
        assert manager.queries.plugin_info(loaded.key)["boot_scope"] == {
            "sealed": True,
            "pending_restart": False,
            "capabilities": [],
        }
    finally:
        manager._boot_scope_sealed = False
        plugin = manager._plugins.get("user/boot-scoped")
        if plugin is not None and plugin.status is PluginStatus.LOADED:
            manager.unload_plugin(plugin.key)
        platform_registry.unregister("boot-scoped-platform")


def test_mcp_tool_list_changes_publish_snapshot_without_health_churn(tmp_path):
    manager = PluginManager(
        Settings(agent_data_dir=tmp_path / "data", plugin_worker_isolation=False),
        plugin_dirs=[],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )
    name = "mcp__demo__lookup"

    async def lookup():
        return "ok"

    tool_registry.register(ToolEntry(
        name=name,
        description="demo MCP tool",
        schema={"type": "object", "properties": {}},
        handler=lookup,
    ))
    try:
        manager.refresh_mcp_tools("demo", "mcp:demo:r1", {name})
        published_revision = manager.capability_store.current.revision
        route = manager.capability_store.current.view().resolve(CapabilityKind.TOOL, name)

        manager.refresh_mcp_tools("demo", "mcp:demo:r1", {name})

        assert route is not None
        assert route.owner == "configured-mcp"
        assert manager.capability_store.current.revision == published_revision

        tool_registry.unregister(name)
        manager.refresh_mcp_tools("demo", "mcp:demo:r1", set())
        assert manager.capability_store.current.view().resolve(CapabilityKind.TOOL, name) is None
    finally:
        tool_registry.unregister(name)
