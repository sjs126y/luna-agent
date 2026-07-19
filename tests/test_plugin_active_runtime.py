import asyncio
import base64
from pathlib import Path

import pytest

from luna_agent.config import Settings
from luna_agent.artifacts import ArtifactStore
from luna_agent.db.database import Database
from luna_agent.plugins import PluginManager, PluginStatus
from luna_agent.plugins.active import (
    ActiveRuntimeControl,
    ActiveResourceRequest,
    ActiveRunnerState,
    ActiveWakeReason,
    PluginGenerationScope,
)
from luna_agent.tools.entry import ToolArtifact, ToolEntry, ToolHandlerOutput
from luna_agent.tools.registry import tool_registry
from luna_agent.plugins.runtime import CapabilityKind


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
async def test_active_runtime_wakeup_supports_timeout_manual_and_stop():
    control = ActiveRuntimeControl(
        plugin=object(),
        scope=PluginGenerationScope(),
    )

    assert await control.wait_for_wakeup(timeout=0) is ActiveWakeReason.TIMER

    control.wake("manual")
    assert await control.wait_for_wakeup(timeout=1) is ActiveWakeReason.MANUAL

    control.request_stop()
    assert await control.wait_for_wakeup(timeout=1) is ActiveWakeReason.STOP


@pytest.mark.asyncio
async def test_active_runtime_wakeup_coalesces_pending_signals():
    control = ActiveRuntimeControl(
        plugin=object(),
        scope=PluginGenerationScope(),
    )

    control.wake("internal")
    control.wake("manual")
    assert await control.wait_for_wakeup(timeout=1) is ActiveWakeReason.INTERNAL
    assert await control.wait_for_wakeup(timeout=0) is ActiveWakeReason.TIMER


def _write_reload_active_plugin(
    root: Path,
    version: str,
    *,
    fail_before_ready: bool = False,
    crash_once_after_ready: bool = False,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.yaml").write_text(
        "\n".join((
            "key: user/active-reload",
            "name: Active Reload",
            "version: 1.0.0",
            "entrypoint: active_reload:register",
            "provides: [active, tools]",
            "enabled_by_default: true",
        )),
        encoding="utf-8",
    )
    (root / "active_reload.py").write_text(
        "\n".join((
            "import asyncio",
            "from luna_agent.plugins import ActiveResourceRequest",
            "from luna_agent.tools.entry import ToolEntry",
            f"VERSION = {version!r}",
            f"FAIL = {fail_before_ready!r}",
            f"CRASH_ONCE = {crash_once_after_ready!r}",
            "ATTEMPTS = 0",
            "",
            "async def value():",
            "    return VERSION",
            "",
            "async def run(ctx):",
            "    global ATTEMPTS",
            "    ATTEMPTS += 1",
            "    ctx.resources.storage.write_text('runner.txt', VERSION)",
            "    if FAIL:",
            "        raise RuntimeError('candidate startup failed')",
            "    await ctx.runtime.ready()",
            "    if CRASH_ONCE and ATTEMPTS == 1:",
            "        raise RuntimeError('crash once')",
            "    while not ctx.runtime.stop_requested:",
            "        await ctx.runtime.wait_until_resumed()",
            "        await asyncio.sleep(0.01)",
            "",
            "def register(ctx):",
            "    ctx.register.tool(ToolEntry(",
            "        name='active_reload_value',",
            "        description='Return active version',",
            "        schema={'type': 'object', 'properties': {}},",
            "        handler=value,",
            "    ))",
            "    ctx.register.active(run=run, resources=ActiveResourceRequest())",
        )),
        encoding="utf-8",
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
        first_context = resources.tool._execution_context(session_key="wechat:c1:u1")
        second_context = resources.tool._execution_context(session_key="wechat:c1:u1")
        assert first_context._hook_turn_id != second_context._hook_turn_id
        assert first_context._artifact_owner_id == plugin.key
        with pytest.raises(PermissionError, match="not allowlisted"):
            await resources.tool.call("read", {"path": "."})
    finally:
        tool_registry.unregister("active_lookup_test")


@pytest.mark.asyncio
async def test_active_tool_artifacts_have_plugin_owner_and_per_call_scope(tmp_path):
    plugin_root = tmp_path / "plugins" / "active"
    _write_active_plugin(plugin_root)
    manager = _manager(tmp_path, plugin_root)
    plugin = manager.load_plugin("user/active-test")
    db = Database(tmp_path / "artifact-state.db")
    await db.initialize()
    store = ArtifactStore(tmp_path / "artifacts", db, max_artifacts_per_turn=10)
    await store.initialize()
    manager._artifact_store = store

    async def artifact():
        return ToolHandlerOutput(
            text="created",
            artifacts=[ToolArtifact(
                kind="file",
                name="active.txt",
                mime_type="text/plain",
                data=base64.b64encode(b"active").decode(),
            )],
        )

    tool_registry.register(ToolEntry(
        name="active_artifact_test",
        description="Create an active plugin artifact",
        schema={"type": "object", "properties": {}},
        handler=artifact,
        permission_category="read",
    ))
    try:
        resources = manager.plugin_resource_facade(
            plugin,
            ActiveResourceRequest(tools=("active_artifact_test",)),
        )
        refs = []
        for _ in range(11):
            result = await resources.tool.call(
                "active_artifact_test",
                session_key="wechat:c1:u1",
            )
            assert result.status == "success"
            assert len(result.artifacts) == 1
            refs.append(result.artifacts[0])

        assert {ref.owner_id for ref in refs} == {plugin.key}
        assert len({ref.turn_id for ref in refs}) == 11
    finally:
        tool_registry.unregister("active_artifact_test")
        await db.close()


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

    await manager.start_active_plugins()
    assert plugin.active_runner.control.state is ActiveRunnerState.ACTIVE
    assert plugin.active_runner.root_task is not None

    await manager.stop_active_plugins()
    assert plugin.active_runner.control.state is ActiveRunnerState.STOPPED


@pytest.mark.asyncio
async def test_active_toggle_is_separate_from_plugin_enabled_state(tmp_path):
    plugin_root = tmp_path / "plugins" / "active"
    _write_active_plugin(plugin_root)
    manager = _manager(tmp_path, plugin_root)
    plugin = manager.load_plugin("user/active-test")
    await manager.start_active_plugins()

    assert plugin.enabled is True
    assert plugin.active_enabled is False
    assert plugin.active_runner.root_task is None

    await manager.set_active_enabled(plugin.key, True)
    assert plugin.enabled is True
    assert plugin.active_enabled is True
    assert plugin.active_runner.control.state is ActiveRunnerState.ACTIVE

    await manager.set_active_enabled(plugin.key, False)
    assert plugin.enabled is True
    assert plugin.active_enabled is False
    assert plugin.active_runner.control.state is ActiveRunnerState.STOPPED

    await manager.stop_active_plugins()


def test_active_resource_request_validates_mcp_readiness_declarations():
    with pytest.raises(ValueError, match="must also declare tools"):
        ActiveResourceRequest(required_mcp_servers=("missing",))
    with pytest.raises(ValueError, match="both required and optional"):
        ActiveResourceRequest(
            mcp={"demo": ("lookup",)},
            required_mcp_servers=("demo",),
            optional_mcp_servers=("demo",),
        )


@pytest.mark.asyncio
async def test_active_reload_commits_ready_generation_and_revision_atomically(tmp_path):
    plugin_root = tmp_path / "plugins" / "active-reload"
    _write_reload_active_plugin(plugin_root, "v1")
    manager = _manager(
        tmp_path,
        plugin_root,
        plugins_config={"user/active-reload": {"active": {"enabled": True}}},
    )
    first = manager.load_plugin("user/active-reload")
    await manager.start_active_plugins()
    old_revision = first.data_revision_id
    old_lease = await manager.capability_store.acquire()
    old_route = old_lease.view().resolve(CapabilityKind.TOOL, "active_reload_value")
    old_entry = manager.capability_payload(old_route.binding_id)

    _write_reload_active_plugin(plugin_root, "v2")
    second = await manager.reload_plugin_runtime("user/active-reload")
    new_route = manager.capability_store.current.view().resolve(
        CapabilityKind.TOOL,
        "active_reload_value",
    )
    new_entry = manager.capability_payload(new_route.binding_id)

    assert second is manager._plugins["user/active-reload"]
    assert second.data_revision_id != old_revision
    assert manager.data_revisions.current_revision(second.key) == second.data_revision_id
    assert second.ctx.resources.storage.read_text("runner.txt") == "v2"
    assert await old_entry.handler() == "v1"
    assert await new_entry.handler() == "v2"
    assert first.active_runner.control.state is ActiveRunnerState.STOPPED
    assert second.active_runner.control.state is ActiveRunnerState.ACTIVE

    await old_lease.release()
    await manager.stop_active_plugins()


@pytest.mark.asyncio
async def test_active_reload_failure_restores_previous_runtime_and_data(tmp_path):
    plugin_root = tmp_path / "plugins" / "active-reload"
    _write_reload_active_plugin(plugin_root, "v1")
    manager = _manager(
        tmp_path,
        plugin_root,
        plugins_config={"user/active-reload": {"active": {"enabled": True}}},
    )
    first = manager.load_plugin("user/active-reload")
    await manager.start_active_plugins()
    old_snapshot_revision = manager.capability_store.current.revision
    old_data_revision = manager.data_revisions.current_revision(first.key)

    _write_reload_active_plugin(plugin_root, "broken", fail_before_ready=True)
    with pytest.raises(RuntimeError, match="candidate startup failed"):
        await manager.reload_plugin_runtime("user/active-reload")

    assert manager._plugins[first.key] is first
    assert manager.capability_store.current.revision == old_snapshot_revision
    assert manager.data_revisions.current_revision(first.key) == old_data_revision
    assert first.ctx.resources.storage.read_text("runner.txt") == "v1"
    assert first.active_runner.control.state is ActiveRunnerState.ACTIVE

    await manager.stop_active_plugins()


@pytest.mark.asyncio
async def test_active_runner_restarts_after_runtime_failure(tmp_path):
    plugin_root = tmp_path / "plugins" / "active-reload"
    _write_reload_active_plugin(plugin_root, "v1", crash_once_after_ready=True)
    manager = _manager(
        tmp_path,
        plugin_root,
        plugins_config={
            "user/active-reload": {
                "active": {
                    "enabled": True,
                    "restart_backoff_seconds": [0],
                }
            }
        },
    )
    plugin = manager.load_plugin("user/active-reload")

    await manager.start_active_plugins()
    for _ in range(100):
        if (
            plugin.active_restart_count == 1
            and plugin.active_runner.control.state is ActiveRunnerState.ACTIVE
        ):
            break
        await asyncio.sleep(0.01)

    assert plugin.active_restart_count == 1
    assert plugin.active_runner.control.state is ActiveRunnerState.ACTIVE
    assert plugin.active_runner.control.restart_count == 1

    await manager.stop_active_plugins()
