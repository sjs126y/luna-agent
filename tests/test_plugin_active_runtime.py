import asyncio
from pathlib import Path

import pytest

from personal_agent.config import Settings
from personal_agent.plugins import PluginManager, PluginStatus
from personal_agent.plugins.active import (
    ActiveResourceRequest,
    ActiveRunnerState,
    PluginGenerationScope,
)
from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry


def _write_active_plugin(root: Path, *, provides: str = "[active]", duplicate: bool = False) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.yaml").write_text(
        "\n".join((
            "key: user/active-test",
            "name: Active Test",
            "version: 1.0.0",
            "entrypoint: active_test:register",
            f"provides: {provides}",
            "enabled_by_default: true",
        )),
        encoding="utf-8",
    )
    registrations = [
        "    ctx.register.active(run=run, startup_timeout=0.5, shutdown_timeout=0.5)",
    ]
    if duplicate:
        registrations.append(
            "    ctx.register.active(run=run, startup_timeout=0.5, shutdown_timeout=0.5)"
        )
    (root / "active_test.py").write_text(
        "\n".join((
            "import asyncio",
            "",
            "async def run(ctx):",
            "    await ctx.runtime.ready()",
            "    while not ctx.runtime.stop_requested:",
            "        await asyncio.sleep(0)",
            "",
            "def register(ctx):",
            *registrations,
        )),
        encoding="utf-8",
    )


def _manager(
    tmp_path: Path,
    plugin_root: Path,
    *,
    plugins_config: dict | None = None,
) -> PluginManager:
    return PluginManager(
        Settings(
            agent_data_dir=tmp_path / "data",
            plugins_dirs=[plugin_root.parent],
            plugins_config=plugins_config or {},
        ),
        plugin_dirs=[plugin_root.parent],
        state_path=tmp_path / "state.json",
        include_builtin=False,
    )


@pytest.mark.asyncio
async def test_generation_scope_closes_in_reverse_order_and_collects_failures():
    scope = PluginGenerationScope()
    order = []

    async def close_second():
        order.append("second")
        raise RuntimeError("close failed")

    scope.defer("first", lambda: order.append("first"))
    scope.defer("second", close_second)

    failures = await scope.aclose()

    assert order == ["second", "first"]
    assert [(item.name, item.error) for item in failures] == [
        ("second", "RuntimeError: close failed")
    ]
    assert await scope.aclose() == failures


def test_active_registration_is_collected_without_starting_runner(tmp_path):
    plugin_root = tmp_path / "plugins" / "active"
    _write_active_plugin(plugin_root)
    plugin = _manager(tmp_path, plugin_root).load_plugin("user/active-test")

    assert plugin.status is PluginStatus.LOADED
    assert plugin.active_registration is not None
    assert plugin.active_runner is not None
    assert plugin.active_runner.root_task is None
    assert plugin.active_runner.control.state is ActiveRunnerState.DISABLED


@pytest.mark.parametrize(
    ("provides", "duplicate", "message"),
    [
        ("[]", False, "must declare provides"),
        ("[active]", True, "only one active runner"),
    ],
)
def test_active_registration_enforces_manifest_and_single_root(
    tmp_path, provides, duplicate, message
):
    plugin_root = tmp_path / "plugins" / "invalid-active"
    _write_active_plugin(plugin_root, provides=provides, duplicate=duplicate)
    plugin = _manager(tmp_path, plugin_root).load_plugin("user/active-test")

    assert plugin.status is PluginStatus.ERROR
    assert message in str(plugin.error)


@pytest.mark.asyncio
async def test_active_runner_waits_for_commit_and_stops_cleanly(tmp_path):
    plugin_root = tmp_path / "plugins" / "active"
    _write_active_plugin(plugin_root)
    plugin = _manager(tmp_path, plugin_root).load_plugin("user/active-test")
    runner = plugin.active_runner

    task = runner.start()
    await runner.wait_ready()

    assert runner.control.state is ActiveRunnerState.READY
    assert not task.done()
    runner.control.commit()
    await asyncio.sleep(0)
    assert runner.control.state is ActiveRunnerState.ACTIVE

    await runner.stop()
    assert runner.control.state is ActiveRunnerState.STOPPED
    assert task.done()


@pytest.mark.asyncio
async def test_active_runner_reports_failure_before_ready(tmp_path):
    plugin_root = tmp_path / "plugins" / "active"
    _write_active_plugin(plugin_root)
    plugin = _manager(tmp_path, plugin_root).load_plugin("user/active-test")
    runner = plugin.active_runner

    async def fail(_ctx):
        raise ValueError("startup failed")

    runner.registration = runner.registration.__class__(
        run=fail,
        startup_timeout=0.5,
        shutdown_timeout=0.5,
    )
    runner.start()

    with pytest.raises(RuntimeError, match="failed before readiness.*startup failed"):
        await runner.wait_ready()
    assert runner.control.state is ActiveRunnerState.FAILED


@pytest.mark.asyncio
async def test_active_tool_resource_uses_exact_allowlist_and_execution_pipeline(tmp_path):
    plugin_root = tmp_path / "plugins" / "active"
    _write_active_plugin(plugin_root)
    manager = _manager(tmp_path, plugin_root)
    plugin = manager.load_plugin("user/active-test")
    calls = []

    async def lookup(value: str):
        calls.append(value)
        return f"found:{value}"

    tool_registry.register(ToolEntry(
        name="active_lookup_test",
        description="Lookup a test value",
        schema={"type": "object", "properties": {"value": {"type": "string"}}},
        handler=lookup,
        permission_category="read",
    ))
    try:
        request = ActiveResourceRequest(tools=("active_lookup_test",))
        resources = manager.plugin_resource_facade(plugin, request)

        result = await resources.tool.call("active_lookup_test", {"value": "one"})

        assert result.status == "success"
        assert result.content == "found:one"
        assert calls == ["one"]
        with pytest.raises(PermissionError, match="not allowlisted"):
            await resources.tool.call("read", {"path": "."})
    finally:
        tool_registry.unregister("active_lookup_test")


@pytest.mark.asyncio
async def test_active_tool_resource_hard_denies_destructive_entries(tmp_path):
    plugin_root = tmp_path / "plugins" / "active"
    _write_active_plugin(plugin_root)
    manager = _manager(tmp_path, plugin_root)
    plugin = manager.load_plugin("user/active-test")

    async def mutate():
        raise AssertionError("destructive handler must not run")

    tool_registry.register(ToolEntry(
        name="active_mutation_test",
        description="Mutation test",
        schema={"type": "object", "properties": {}},
        handler=mutate,
        permission_category="default",
        is_destructive=True,
    ))
    try:
        resources = manager.plugin_resource_facade(
            plugin,
            ActiveResourceRequest(tools=("active_mutation_test",)),
        )
        with pytest.raises(PermissionError, match="cannot execute destructive"):
            await resources.tool.call("active_mutation_test")
    finally:
        tool_registry.unregister("active_mutation_test")


@pytest.mark.asyncio
async def test_active_mcp_resource_requires_exact_server_and_tool(tmp_path):
    plugin_root = tmp_path / "plugins" / "active"
    _write_active_plugin(plugin_root)
    manager = _manager(tmp_path, plugin_root)
    plugin = manager.load_plugin("user/active-test")

    async def remote(query: str):
        return query

    public_name = "mcp__demo__lookup"
    tool_registry.register(ToolEntry(
        name=public_name,
        description="MCP-shaped test tool",
        schema={"type": "object", "properties": {}},
        handler=remote,
        toolset="mcp",
        approval_mode="cached",
    ))
    try:
        resources = manager.plugin_resource_facade(
            plugin,
            ActiveResourceRequest(mcp={"demo": ("lookup",)}),
        )
        result = await resources.mcp.call("demo", "lookup", {"query": "ok"})
        assert result.status == "success"
        assert result.content == "ok"
        with pytest.raises(PermissionError, match="not allowlisted"):
            await resources.mcp.call("demo", "delete", {})
    finally:
        tool_registry.unregister(public_name)


def test_generation_bound_resource_handle_expires_with_scope(tmp_path):
    plugin_root = tmp_path / "plugins" / "active"
    _write_active_plugin(plugin_root)
    manager = _manager(tmp_path, plugin_root)
    plugin = manager.load_plugin("user/active-test")
    resources = manager.plugin_resource_facade(plugin, ActiveResourceRequest())
    plugin.generation_scope._closed = True

    with pytest.raises(RuntimeError, match="no longer active"):
        _ = resources.storage


@pytest.mark.asyncio
async def test_active_runner_starts_only_when_gateway_owner_is_running(tmp_path):
    plugin_root = tmp_path / "plugins" / "active"
    _write_active_plugin(plugin_root)
    manager = _manager(
        tmp_path,
        plugin_root,
        plugins_config={"user/active-test": {"active": {"enabled": True}}},
    )
    plugin = manager.load_plugin("user/active-test")

    assert plugin.active_enabled is True
    assert plugin.active_runner.root_task is None

    await manager.runtime_manager.start_active()
    assert plugin.active_runner.control.state is ActiveRunnerState.ACTIVE
    assert plugin.active_runner.root_task is not None

    await manager.runtime_manager.stop_active()
    assert plugin.active_runner.control.state is ActiveRunnerState.STOPPED


@pytest.mark.asyncio
async def test_active_toggle_is_separate_from_plugin_enabled_state(tmp_path):
    plugin_root = tmp_path / "plugins" / "active"
    _write_active_plugin(plugin_root)
    manager = _manager(tmp_path, plugin_root)
    plugin = manager.load_plugin("user/active-test")
    await manager.runtime_manager.start_active()

    assert plugin.enabled is True
    assert plugin.active_enabled is False
    assert plugin.active_runner.root_task is None

    await manager.runtime_manager.set_active(plugin.key, True)
    assert plugin.enabled is True
    assert plugin.active_enabled is True
    assert plugin.active_runner.control.state is ActiveRunnerState.ACTIVE

    await manager.runtime_manager.set_active(plugin.key, False)
    assert plugin.enabled is True
    assert plugin.active_enabled is False
    assert plugin.active_runner.control.state is ActiveRunnerState.STOPPED

    await manager.runtime_manager.stop_active()


def test_active_resource_request_validates_mcp_readiness_declarations():
    with pytest.raises(ValueError, match="must also declare tools"):
        ActiveResourceRequest(required_mcp_servers=("missing",))
    with pytest.raises(ValueError, match="both required and optional"):
        ActiveResourceRequest(
            mcp={"demo": ("lookup",)},
            required_mcp_servers=("demo",),
            optional_mcp_servers=("demo",),
        )
