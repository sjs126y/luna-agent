import asyncio
from pathlib import Path

import pytest

from personal_agent.config import Settings
from personal_agent.plugins import PluginManager, PluginStatus
from personal_agent.plugins.runtime import CapabilityKind, PluginRuntimeState


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
            "from personal_agent.tools.entry import ToolEntry",
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


@pytest.mark.asyncio
async def test_reload_publishes_new_routes_and_drains_old_runtime(tmp_path):
    plugin_dir = tmp_path / "plugins" / "hot_reload"
    _write_plugin(plugin_dir, "version-one")
    manager = PluginManager(
        Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[plugin_dir.parent]),
        plugin_dirs=[plugin_dir.parent],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )
    first = manager.load_plugin("user/hot-reload")
    assert first.status is PluginStatus.LOADED

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

    await old_lease.release()
    await asyncio.sleep(0)

    assert first.runtime_state is PluginRuntimeState.STOPPED
    assert manager.capability_payload(old_route.binding_id) is None
