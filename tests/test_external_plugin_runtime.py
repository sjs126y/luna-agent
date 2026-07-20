from __future__ import annotations

from pathlib import Path

import pytest

from luna_agent.config import Settings
from luna_agent.plugins import PluginManager, PluginStatus
from luna_agent.tools.registry import tool_registry


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["process-only", "bwrap"])
async def test_external_plugin_loads_and_invokes_through_worker(
    tmp_path: Path,
    backend: str,
) -> None:
    root = tmp_path / "plugins" / "demo"
    root.mkdir(parents=True)
    (root / "plugin.yaml").write_text(
        "\n".join((
            "schema_version: 1",
            "key: user/worker-demo",
            "name: Worker Demo",
            "version: 1.0.0",
            "entrypoint: demo:register",
            "requires:",
            "  sdk: '>=0.2,<1'",
            "provides: [tools]",
            "enabled_by_default: true",
        )),
        encoding="utf-8",
    )
    (root / "demo.py").write_text(
        "from luna_agent_plugin_sdk import ToolEntry\n"
        "async def echo(text=''): return {'worker': True, 'text': text}\n"
        "def register(ctx):\n"
        "    ctx.register.tool(ToolEntry(name='worker_demo_echo', description='echo', "
        "schema={'type':'object'}, handler=echo))\n",
        encoding="utf-8",
    )
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[root.parent],
        plugins_enabled=["user/worker-demo"],
        plugin_worker_isolation=True,
        plugin_sandbox_backend=backend,
    )
    manager = PluginManager(
        settings,
        plugin_dirs=[root.parent],
        include_builtin=False,
        state_path=tmp_path / "state.json",
    )
    manager.discover()
    plugin = manager.load_plugin("user/worker-demo")
    try:
        assert plugin.status is PluginStatus.LOADED
        assert plugin.module is None
        assert plugin.worker is not None
        assert plugin.environment_id
        assert plugin.sandbox_backend == backend
        entry = tool_registry.get("worker_demo_echo")
        assert entry is not None
        assert await entry.handler(text="ok") == {"worker": True, "text": "ok"}
    finally:
        manager.unload_plugin("user/worker-demo")
        tool_registry.unregister("worker_demo_echo")


@pytest.mark.asyncio
async def test_external_active_plugin_uses_host_runtime_control(tmp_path: Path) -> None:
    root = tmp_path / "plugins" / "active"
    root.mkdir(parents=True)
    (root / "plugin.yaml").write_text(
        "\n".join((
            "schema_version: 1",
            "key: user/worker-active",
            "name: Worker Active",
            "version: 1.0.0",
            "entrypoint: active:register",
            "requires:",
            "  sdk: '>=0.2,<1'",
            "provides: [active]",
            "enabled_by_default: true",
        )),
        encoding="utf-8",
    )
    (root / "active.py").write_text(
        "from luna_agent_plugin_sdk import ActiveResourceRequest\n"
        "async def run(ctx):\n"
        "    await ctx.runtime.ready()\n"
        "    await ctx.runtime.wait_until_stopped()\n"
        "def register(ctx):\n"
        "    ctx.register.active(run=run, resources=ActiveResourceRequest())\n",
        encoding="utf-8",
    )
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[root.parent],
        plugins_enabled=["user/worker-active"],
        plugins_config={"user/worker-active": {"active": {"enabled": True}}},
        plugin_worker_isolation=True,
        plugin_sandbox_backend="process-only",
    )
    manager = PluginManager(
        settings,
        plugin_dirs=[root.parent],
        include_builtin=False,
        state_path=tmp_path / "state.json",
    )
    manager.discover()
    plugin = manager.load_plugin("user/worker-active")
    assert plugin.status is PluginStatus.LOADED
    await manager.start_active_plugins()
    try:
        assert plugin.active_runner.control.safe_summary()["state"] == "active"
    finally:
        await manager.stop_active_plugins()
        await manager.unload_plugin_runtime("user/worker-active")
