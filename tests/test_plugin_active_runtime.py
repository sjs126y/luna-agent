import asyncio
from pathlib import Path

import pytest

from personal_agent.config import Settings
from personal_agent.plugins import PluginManager, PluginStatus
from personal_agent.plugins.active import ActiveRunnerState, PluginGenerationScope


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


def _manager(tmp_path: Path, plugin_root: Path) -> PluginManager:
    return PluginManager(
        Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[plugin_root.parent]),
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
