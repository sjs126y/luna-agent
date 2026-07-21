from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from luna_agent.config import Settings
from luna_agent.plugins import PluginManager, PluginStatus
from luna_agent.plugins.runtime.external_service import PluginHostWorkspaceService
from luna_agent.skills.registry import skill_registry
from luna_agent.tools.registry import tool_registry
from luna_agent.workflow.registry import workflow_registry


class _WorkspacePlugin:
    key = "integrations/codex-bridge"
    manifest = SimpleNamespace(path=Path(__file__))

    def __init__(self, workspaces):
        self.active_registration = SimpleNamespace(
            resources=SimpleNamespace(workspaces=tuple(workspaces)),
        )


class _WorkspaceManager:
    def __init__(self, config):
        self.settings = SimpleNamespace(plugins_config={
            "integrations/codex-bridge": config,
        })


@pytest.mark.asyncio
async def test_workspace_service_infers_the_only_declared_name(tmp_path):
    service = PluginHostWorkspaceService(_WorkspaceManager({
        "development_root": str(tmp_path / "workspaces"),
    }))
    plugin = _WorkspacePlugin(["development"])

    result = await service.call(plugin, "create", [], {"workspace": "demo"})

    assert Path(result["path"]) == (tmp_path / "workspaces" / "demo").resolve()


@pytest.mark.asyncio
async def test_workspace_service_requires_name_for_multiple_declarations(tmp_path):
    service = PluginHostWorkspaceService(_WorkspaceManager({
        "host_workspaces": {
            "development": {"root": str(tmp_path / "development")},
            "artifacts": {"root": str(tmp_path / "artifacts")},
        },
    }))
    plugin = _WorkspacePlugin(["development", "artifacts"])

    with pytest.raises(PermissionError, match="Plugin workspace is not declared"):
        await service.call(plugin, "create", [], {"workspace": "demo"})


def test_external_runtime_normalizes_codex_paths_on_host(tmp_path: Path, monkeypatch) -> None:
    from luna_agent.plugins.runtime.external_service import ExternalPluginRuntimeService

    settings = SimpleNamespace(
        agent_data_dir=tmp_path / "data",
        plugins_config={},
    )
    manager = SimpleNamespace(settings=settings)
    service = ExternalPluginRuntimeService(manager, tmp_path / "plugin-state")
    plugin = SimpleNamespace(key="integrations/codex-bridge")
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "source-codex"
    source.mkdir()
    (source / "auth.json").write_text("{}", encoding="utf-8")

    normalized = service.normalized_config(plugin, {
        "source_codex_home": str(source),
        "runtime_codex_home": "./runtime-codex",
    })
    runtime_home = (Path.cwd() / "runtime-codex").resolve()
    try:
        assert normalized["runtime_codex_home"] == str(runtime_home)
        assert normalized["source_codex_home"] == str(source.resolve())
        assert (runtime_home / "auth.json").read_text(encoding="utf-8") == "{}"
        assert (runtime_home / "auth.json").stat().st_mode & 0o777 == 0o600
    finally:
        (runtime_home / "auth.json").unlink(missing_ok=True)
        runtime_home.rmdir()


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
        "from pathlib import Path\n"
        "from luna_agent_plugin_sdk import ToolEntry, ToolResourceBinding\n"
        "async def echo(text=''): return {'worker': True, 'text': text}\n"
        "async def read_path(path): return Path(path).read_text(encoding='utf-8')\n"
        "def register(ctx):\n"
        "    ctx.register.tool(ToolEntry(name='worker_demo_echo', description='echo', "
        "schema={'type':'object'}, handler=echo))\n"
        "    ctx.register.tool(ToolEntry(name='worker_demo_read', description='read', "
        "schema={'type':'object'}, handler=read_path, resource_bindings=("
        "ToolResourceBinding('filesystem', 'path', 'read', 'test input'),)))\n",
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
        source = tmp_path / "outside.txt"
        source.write_text("approved input", encoding="utf-8")
        read_entry = tool_registry.get("worker_demo_read")
        assert read_entry is not None
        assert read_entry.resource_resolver({"path": str(source)})[0].resource == str(source)
        assert await read_entry.handler(path=str(source)) == "approved input"
    finally:
        manager.unload_plugin("user/worker-demo")
        tool_registry.unregister("worker_demo_echo")
        tool_registry.unregister("worker_demo_read")


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


@pytest.mark.asyncio
async def test_external_plugin_replays_explicit_skill_and_workflow(tmp_path: Path) -> None:
    root = tmp_path / "plugins" / "capabilities"
    root.mkdir(parents=True)
    (root / "notes.md").write_text("# Worker skill\n", encoding="utf-8")
    (root / "plugin.yaml").write_text(
        "\n".join((
            "schema_version: 1",
            "key: user/worker-capabilities",
            "name: Worker Capabilities",
            "version: 1.0.0",
            "entrypoint: capabilities:register",
            "requires:",
            "  sdk: '>=0.3,<1'",
            "provides: [skills, workflow]",
            "enabled_by_default: true",
        )),
        encoding="utf-8",
    )
    (root / "capabilities.py").write_text(
        "from dataclasses import dataclass, field\n"
        "@dataclass\n"
        "class Skill:\n"
        "    name: str\n"
        "    description: str\n"
        "    path: str\n"
        "    triggers: list[str] = field(default_factory=list)\n"
        "@dataclass\n"
        "class Workflow:\n"
        "    name: str\n"
        "    description: str\n"
        "    fn: object\n"
        "    phases: list[str] = field(default_factory=list)\n"
        "    when_to_use: str = ''\n"
        "async def run(value=None): return {'worker': True, 'value': value}\n"
        "def register(ctx):\n"
        "    ctx.register.skill(Skill('worker_skill', 'skill', str(ctx.resolve_path('notes.md'))))\n"
        "    ctx.register.workflow(Workflow('worker_workflow', 'workflow', run, ['run'], 'test'))\n",
        encoding="utf-8",
    )
    manager = PluginManager(
        Settings(
            agent_data_dir=tmp_path / "data",
            plugins_dirs=[root.parent],
            plugins_enabled=["user/worker-capabilities"],
            plugin_worker_isolation=True,
            plugin_sandbox_backend="process-only",
        ),
        plugin_dirs=[root.parent],
        include_builtin=False,
        state_path=tmp_path / "state.json",
    )
    manager.discover()
    plugin = manager.load_plugin("user/worker-capabilities")
    try:
        assert plugin.status is PluginStatus.LOADED
        assert skill_registry.get("worker_skill") is not None
        workflow = workflow_registry.get("worker_workflow")
        assert workflow is not None
        assert await workflow.fn("ok") == {"worker": True, "value": "ok"}
    finally:
        manager.unload_plugin("user/worker-capabilities")
        skill_registry.unregister("worker_skill")
        workflow_registry.unregister("worker_workflow")


@pytest.mark.asyncio
async def test_external_codex_bridge_registers_without_host_imports(tmp_path: Path) -> None:
    from pathlib import Path as _Path

    plugin_root = _Path(__file__).resolve().parents[1] / "plugins"
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[plugin_root],
        plugins_enabled=["integrations/codex-bridge"],
        plugins_config={
            "integrations/codex-bridge": {
                "command": "sh",
                "source_codex_home": str(tmp_path / "source-codex"),
                "runtime_codex_home": str(tmp_path / "runtime-codex"),
                "development_root": str(tmp_path / "development"),
                "development_spec_path": str(tmp_path / "spec.md"),
                "active": {"enabled": False},
            },
        },
        plugin_worker_isolation=True,
        plugin_sandbox_backend="process-only",
    )
    (tmp_path / "source-codex").mkdir()
    (tmp_path / "spec.md").write_text("plugin spec", encoding="utf-8")
    manager = PluginManager(
        settings,
        plugin_dirs=[plugin_root],
        include_builtin=False,
        state_path=tmp_path / "state.json",
    )
    manager.discover()
    plugin = manager.load_plugin("integrations/codex-bridge")
    try:
        assert plugin.status is PluginStatus.LOADED
        assert plugin.worker is not None
        assert plugin.active_registration is not None
        assert "codex-app-server" in plugin.active_registration.resources.processes
        assert "development" in plugin.active_registration.resources.workspaces
        runtime = manager.queries.plugin_info(plugin.key)["external_runtime"]
        assert runtime["isolated"] is True
        assert runtime["worker"]["running"] is True
        assert runtime["worker"]["pid"]
    finally:
        manager.unload_plugin("integrations/codex-bridge")


@pytest.mark.asyncio
async def test_host_process_port_is_allowlisted_and_bidirectional(tmp_path: Path) -> None:
    from luna_agent.plugins.runtime.external_service import PluginHostProcessService

    class Settings:
        plugins_config = {
            "user/process": {
                "host_processes": {
                    "echo": {
                        "executable": "cat",
                        "args_prefix": [],
                        "cwd": str(tmp_path),
                        "cwd_roots": [str(tmp_path)],
                        "max_instances": 1,
                    }
                }
            }
        }

    class Manager:
        settings = Settings()

    class Registration:
        class Resources:
            processes = ("echo",)
        resources = Resources()

    class Plugin:
        key = "user/process"
        runtime_instance_id = "user-process:one"
        active_registration = Registration()

    service = PluginHostProcessService(Manager())
    port = service.port(Plugin())
    started = await port.start(name="echo", cwd=str(tmp_path))
    process_id = str(started["process_id"])
    try:
        await port.write_line(process_id=process_id, text="hello")
        line = await port.read_line(process_id=process_id)
        assert line["line"] == "hello"
    finally:
        await port.stop(process_id=process_id)


@pytest.mark.asyncio
async def test_codex_host_process_allows_only_declared_codex_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from luna_agent.plugins.runtime.external_service import PluginHostProcessService

    codex_home = tmp_path / "codex-home"
    settings = SimpleNamespace(
        plugins_config={
            "integrations/codex-bridge": {
                "runtime_codex_home": str(codex_home),
                "development_root": str(tmp_path),
                "cwd": str(tmp_path),
            }
        }
    )

    class Manager:
        def __init__(self):
            self.settings = settings
            self.external_runtime = SimpleNamespace(
                normalized_config=lambda _plugin, config: dict(config),
            )

    class Registration:
        class Resources:
            processes = ("codex-app-server",)
        resources = Resources()

    class Plugin:
        key = "integrations/codex-bridge"
        runtime_instance_id = "codex-bridge:test"
        active_registration = Registration()

    class Process:
        pid = 1234
        returncode = None
        stdin = None
        stdout = None
        stderr = None

    monkeypatch.setattr("shutil.which", lambda _command: "/usr/bin/codex")
    captured = {}

    async def fake_create_process(*argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return Process()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_process)
    service = PluginHostProcessService(Manager())
    result = await service.call(
        Plugin(),
        "start",
        [],
        {"name": "codex-app-server", "cwd": str(tmp_path), "env": {"CODEX_HOME": str(codex_home)}},
    )

    assert result["name"] == "codex-app-server"
    assert captured["argv"] == ("/usr/bin/codex", "app-server")
    assert captured["env"]["CODEX_HOME"] == str(codex_home)
    assert "env_allowlist" in service._spec(Plugin(), "codex-app-server")

    with pytest.raises(PermissionError, match="undeclared environment variables"):
        await service.call(
            Plugin(),
            "start",
            [],
            {
                "name": "codex-app-server",
                "cwd": str(tmp_path),
                "env": {"CODEX_HOME": str(codex_home), "SECRET": "nope"},
            },
        )


@pytest.mark.asyncio
async def test_passive_worker_recovers_without_replacing_tool_proxy(tmp_path: Path) -> None:
    root = tmp_path / "plugins" / "recover"
    root.mkdir(parents=True)
    (root / "plugin.yaml").write_text(
        "\n".join((
            "schema_version: 1",
            "key: user/worker-recover",
            "name: Worker Recover",
            "version: 1.0.0",
            "entrypoint: recover:register",
            "requires:",
            "  sdk: '>=0.3,<1'",
            "provides: [tools]",
            "enabled_by_default: true",
        )),
        encoding="utf-8",
    )
    (root / "recover.py").write_text(
        "from luna_agent_plugin_sdk import ToolEntry\n"
        "async def pid():\n"
        "    import os\n"
        "    return os.getpid()\n"
        "def register(ctx):\n"
        "    ctx.register.tool(ToolEntry(name='worker_recover_pid', description='pid', "
        "schema={'type':'object'}, handler=pid))\n",
        encoding="utf-8",
    )
    manager = PluginManager(
        Settings(
            agent_data_dir=tmp_path / "data",
            plugins_dirs=[root.parent],
            plugins_enabled=["user/worker-recover"],
            plugin_worker_isolation=True,
            plugin_sandbox_backend="process-only",
            plugin_worker_restart_backoff=[0],
        ),
        plugin_dirs=[root.parent],
        include_builtin=False,
        state_path=tmp_path / "state.json",
    )
    manager.discover()
    plugin = manager.load_plugin("user/worker-recover")
    entry = tool_registry.get("worker_recover_pid")
    assert entry is not None
    first_pid = await entry.handler()
    assert plugin.worker is not None and plugin.worker.process is not None
    plugin.worker.process.kill()
    for _ in range(300):
        if (
            plugin.worker_state == "running"
            and plugin.worker_restart_count == 1
            and not manager.external_runtime.worker_supervisor.recovery_tasks
        ):
            break
        await asyncio.sleep(0.01)
    try:
        assert plugin.worker_state == "running"
        assert plugin.worker_restart_count == 1
        assert await entry.handler() != first_pid
        assert manager.capability_store.current.revision >= 2
        assert manager.external_runtime.worker_supervisor.health_snapshot()[
            "recovery_task_count"
        ] == 0
    finally:
        manager.unload_plugin("user/worker-recover")
        tool_registry.unregister("worker_recover_pid")


@pytest.mark.asyncio
async def test_passive_worker_opens_circuit_after_repeated_restart_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "plugins" / "circuit"
    root.mkdir(parents=True)
    (root / "plugin.yaml").write_text(
        "\n".join((
            "schema_version: 1",
            "key: user/worker-circuit",
            "name: Worker Circuit",
            "version: 1.0.0",
            "entrypoint: circuit:register",
            "requires:",
            "  sdk: '>=0.3,<1'",
            "provides: []",
            "enabled_by_default: true",
        )),
        encoding="utf-8",
    )
    (root / "circuit.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
    manager = PluginManager(
        Settings(
            agent_data_dir=tmp_path / "data",
            plugins_dirs=[root.parent],
            plugins_enabled=["user/worker-circuit"],
            plugin_worker_isolation=True,
            plugin_sandbox_backend="process-only",
            plugin_worker_restart_backoff=[0],
            plugin_worker_restart_failure_limit=2,
        ),
        plugin_dirs=[root.parent],
        include_builtin=False,
        state_path=tmp_path / "state.json",
    )
    manager.discover()
    plugin = manager.load_plugin("user/worker-circuit")
    assert plugin.worker is not None and plugin.worker.process is not None
    monkeypatch.setattr(
        manager.external_runtime,
        "_spawn_worker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("restart failed")),
    )
    plugin.worker.process.kill()
    for _ in range(300):
        if plugin.worker_circuit_open:
            break
        await asyncio.sleep(0.01)
    try:
        assert plugin.worker_state == "circuit_open"
        assert plugin.worker_circuit_open is True
        assert plugin.worker_restart_count == 1
    finally:
        manager.unload_plugin("user/worker-circuit")
