"""Central host adapter for out-of-process external plugin generations."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
from uuid import uuid4
from pathlib import Path
from typing import Any

from luna_agent_plugin_sdk import (
    ActiveRegistration,
    ActiveResourceRequest,
    ActiveRestartPolicy,
    CommandEntry,
    ResourceRequirement,
    ToolResourceBinding,
    ToolEntry,
)

from luna_agent.plugins.install import PluginEnvironment, PluginEnvironmentManager
from luna_agent.plugins.runtime.sandbox import build_plugin_worker_launch
from luna_agent.plugins.runtime.worker_client import PluginWorkerClient
from luna_agent.plugins.runtime.worker_supervisor import WorkerSupervisor
from luna_agent.skills.entry import SkillEntry
from luna_agent.workflow.registry import WorkflowDef


class ExternalPluginRuntimeService:
    """Own environments, workers, host proxies, and resource RPC for external plugins."""

    def __init__(self, manager, root: Path) -> None:
        self.manager = manager
        self.environments = PluginEnvironmentManager(Path(root) / "environments")
        self.processes = PluginHostProcessService(manager)
        self.workspaces = PluginHostWorkspaceService(manager)
        self.worker_supervisor = WorkerSupervisor(self)

    @property
    def workers(self) -> dict[str, PluginWorkerClient]:
        return self.worker_supervisor.workers

    def prepare_environment(self, plugin) -> PluginEnvironment:
        return self.environments.ensure(plugin.key, plugin.manifest.requires.python)

    def start(
        self,
        plugin,
        *,
        environment: PluginEnvironment,
        config: dict[str, Any],
    ) -> None:
        self.worker_supervisor.start(
            plugin,
            environment=environment,
            config=self.normalized_config(plugin, config),
        )

    def normalized_config(self, plugin, config: dict[str, Any]) -> dict[str, Any]:
        """Normalize host-owned paths before an external Worker starts.

        Isolated Workers run in a different cwd and cannot resolve host-relative
        paths.  Codex Bridge is the only plugin whose registration passes a
        host process home through MCP, so normalize and seed that home here.
        """
        normalized = dict(config or {})
        if plugin.key != "integrations/codex-bridge":
            return normalized

        settings = self.manager.settings
        runtime_value = normalized.get("runtime_codex_home")
        runtime_home = Path(
            runtime_value
            or (getattr(settings, "agent_data_dir", Path("data")) / "codex-bridge")
        ).expanduser()
        if not runtime_home.is_absolute():
            runtime_home = (Path.cwd() / runtime_home).resolve()
        else:
            runtime_home = runtime_home.resolve()

        source_home = Path(
            normalized.get("source_codex_home") or (Path.home() / ".codex")
        ).expanduser()
        if not source_home.is_absolute():
            source_home = (Path.cwd() / source_home).resolve()
        else:
            source_home = source_home.resolve()

        runtime_home.mkdir(parents=True, exist_ok=True)
        PluginHostProcessService._seed_directory(source_home, runtime_home)
        normalized["runtime_codex_home"] = str(runtime_home)
        normalized["source_codex_home"] = str(source_home)
        return normalized

    def _spawn_worker(
        self,
        plugin,
        *,
        environment: PluginEnvironment,
        config: dict[str, Any],
        host_loop: asyncio.AbstractEventLoop | None = None,
    ):
        if plugin.manifest.path is None:
            raise ValueError(f"Plugin package root is unavailable: {plugin.key}")
        if plugin.data_path is None:
            raise RuntimeError(f"Plugin data revision is unavailable: {plugin.key}")
        backend = str(
            getattr(self.manager.settings, "plugin_sandbox_backend", "auto") or "auto"
        )
        allow_network = bool(
            getattr(self.manager.settings, "plugin_worker_allow_network", False)
        )
        launch = build_plugin_worker_launch(
            python=environment.python,
            plugin_root=plugin.manifest.path,
            environment_root=environment.root,
            data_root=Path(plugin.data_path),
            allow_network=allow_network,
            backend=backend,
            plugin_key=plugin.key,
            runtime_instance_id=plugin.runtime_instance_id,
        )
        worker = PluginWorkerClient(
            cwd=launch.cwd,
            argv=launch.argv,
            env=self._worker_env(plugin),
            startup_timeout=float(
                getattr(self.manager.settings, "plugin_worker_startup_timeout", 45.0)
            ),
            shutdown_timeout=float(
                getattr(self.manager.settings, "plugin_worker_shutdown_timeout", 10.0)
            ),
            process_factory=launch.process_factory,
            on_exit=lambda exited, summary, _plugin=plugin: (
                self.worker_supervisor.worker_exited(_plugin, exited, summary)
            ),
            host_loop=host_loop,
        )
        worker.set_resource_handler(
            lambda payload: self._resource_call(plugin, payload)
        )
        try:
            result = worker.start({
                "plugin_key": plugin.key,
                "generation_id": plugin.generation_id,
                "runtime_instance_id": plugin.runtime_instance_id,
                "plugin_root": str(plugin.manifest.path.resolve()),
                "data_root": str(Path(plugin.data_path).resolve()),
                "entrypoint": plugin.manifest.entrypoint,
                "config": config,
            })
            return worker, result, launch
        except Exception as exc:
            detail = worker.last_stderr.strip()
            worker.stop()
            if launch.cleanup is not None:
                launch.cleanup()
            if detail:
                raise RuntimeError(
                    f"External plugin worker failed: {exc}; stderr: {detail[-8000:]}"
                ) from exc
            raise

    def _worker_exited(
        self,
        plugin,
        worker: PluginWorkerClient,
        summary: dict[str, Any],
    ) -> None:
        self.worker_supervisor.worker_exited(plugin, worker, summary)

    async def _recover_worker(
        self,
        plugin,
        exited_worker: PluginWorkerClient,
        summary: dict[str, Any],
    ) -> None:
        await self.worker_supervisor._recover(plugin, exited_worker, summary)

    def _current_worker(self, plugin) -> PluginWorkerClient:
        return self.worker_supervisor.current_worker(plugin)

    async def _call_worker(self, plugin, method: str, payload: dict[str, Any], *, timeout: float = 30.0):
        return await self._current_worker(plugin).call(method, payload, timeout=timeout)

    def stop(self, plugin) -> None:
        self.worker_supervisor.stop(plugin)

    def summary(self, plugin) -> dict[str, Any]:
        return self.worker_supervisor.summary(plugin)

    def close(self, plugins) -> None:
        self.worker_supervisor.close(plugins)

    async def aclose(self, plugins) -> None:
        await self.worker_supervisor.aclose(plugins)

    def _register_capabilities(self, plugin, capabilities: dict[str, Any]) -> None:
        ctx = plugin.ctx
        if ctx is None or plugin.worker is None:
            raise RuntimeError(f"Plugin host proxy context is unavailable: {plugin.key}")
        for descriptor in capabilities.get("tools", []):
            value = _plain(descriptor)
            timeout = float(value.get("timeout_seconds") or 120.0)
            bindings = tuple(
                ToolResourceBinding(
                    kind=str(item.get("kind") or "filesystem"),
                    argument=str(item.get("argument") or ""),
                    access=str(item.get("access") or "read"),
                    reason=str(item.get("reason") or ""),
                )
                for item in (value.get("resource_bindings") or [])
            )

            async def handler(
                _handler_id=value["handler_id"],
                _timeout=timeout,
                _bindings=bindings,
                **kwargs,
            ):
                invocation = _invocation_metadata()
                rewritten = dict(kwargs)
                if _bindings:
                    rewritten = self._stage_tool_inputs(plugin, rewritten, _bindings)
                return await self._call_worker(
                    plugin,
                    "invoke",
                    {
                        "handler_id": _handler_id,
                        "kwargs": rewritten,
                        "invocation": invocation,
                    },
                    timeout=_timeout,
                )

            def resource_resolver(input_: dict[str, Any], _bindings=bindings):
                requirements = []
                for binding in _bindings:
                    raw = input_.get(binding.argument)
                    if raw in (None, ""):
                        continue
                    requirements.append(ResourceRequirement(
                        kind=binding.kind,
                        resource=str(raw),
                        access=binding.access,
                        reason=binding.reason,
                    ))
                return requirements

            ctx._register_tool(ToolEntry(
                name=str(value["name"]),
                description=str(value.get("description") or ""),
                schema=dict(value.get("schema") or {}),
                handler=handler,
                toolset=str(value.get("toolset") or "general"),
                permission_category=str(value.get("permission_category") or "default"),
                tags=list(value.get("tags") or []),
                risk_level=str(value.get("risk_level") or "low"),
                usage_hint=str(value.get("usage_hint") or ""),
                approval_mode=str(value.get("approval_mode") or "inherit"),
                idempotent=value.get("idempotent"),
                is_parallel_safe=bool(value.get("is_parallel_safe", True)),
                is_destructive=bool(value.get("is_destructive", False)),
                report_as_tool=bool(value.get("report_as_tool", True)),
                timeout_seconds=value.get("timeout_seconds"),
                resource_resolver=resource_resolver if bindings else None,
                resource_bindings=bindings,
            ))
        for descriptor in capabilities.get("skills", []):
            value = _plain(descriptor)
            ctx._register_skill(SkillEntry(
                name=str(value.get("name") or ""),
                description=str(value.get("description") or ""),
                path=str(value.get("path") or ""),
                triggers=list(value.get("triggers") or []),
                plugin_key=plugin.key,
                allowed_root=str(value.get("allowed_root") or plugin.manifest.path or ""),
            ))
        for descriptor in capabilities.get("workflows", []):
            value = _plain(descriptor)
            handler_id = str(value.get("handler_id") or value.get("name") or "")

            async def workflow(args=None, _handler_id=handler_id):
                return await self._call_worker(
                    plugin,
                    "invoke",
                    {"handler_id": _handler_id, "args": [args]},
                )

            ctx._register_workflow(WorkflowDef(
                name=str(value.get("name") or ""),
                description=str(value.get("description") or ""),
                fn=workflow,
                phases=list(value.get("phases") or []),
                when_to_use=str(value.get("when_to_use") or ""),
            ))
        for relative_path in capabilities.get("skill_directories", []):
            ctx._register_skills(str(relative_path))
        for relative_path in capabilities.get("mcp_files", []):
            ctx._register_mcp(str(relative_path))
        for descriptor in capabilities.get("mcp_servers", []):
            ctx._register_mcp_server(_plain(descriptor))
        for descriptor in capabilities.get("hooks", []):
            value = _plain(descriptor)

            async def callback(*args, _handler_id=value["handler_id"], **kwargs):
                return await self._call_worker(plugin, "invoke", {
                    "handler_id": _handler_id,
                    "args": list(args),
                    "kwargs": kwargs,
                })

            ctx._register_hook(
                str(value["event"]),
                callback,
                int(value.get("priority") or 100),
                name=str(value.get("name") or ""),
                matcher=str(value.get("matcher") or "*"),
                timeout=value.get("timeout"),
            )
        for descriptor in capabilities.get("commands", []):
            value = _plain(descriptor)

            async def command(_handler_id=value["handler_id"], **kwargs):
                return await self._call_worker(plugin, "invoke", {
                    "handler_id": _handler_id,
                    "kwargs": kwargs,
                })

            ctx._register_command(CommandEntry(
                name=str(value["name"]),
                description=str(value.get("description") or ""),
                handler=command,
                scope=str(value.get("scope") or "slash"),
            ))
        active = list(capabilities.get("active", []))
        if active:
            self._register_active(plugin, _plain(active[0]))

    def _register_active(self, plugin, descriptor: dict[str, Any]) -> None:
        if "active" not in set(plugin.manifest.provides or []):
            raise ValueError("Plugin must declare provides: [active] before registering a runner")
        if plugin.worker is None:
            raise RuntimeError("Plugin worker is unavailable")
        resources = _active_resources(_plain(descriptor.get("resources") or {}))
        run_id = str(descriptor["run"])

        async def run(_ctx) -> None:
            await self._call_worker(plugin, "active.start", {"handler_id": run_id})
            await self._call_worker(
                plugin,
                "active.wait",
                {},
                timeout=365 * 24 * 3600.0,
            )

        def lifecycle(name: str):
            handler_id = str(descriptor.get(name) or "")
            if not handler_id:
                return None

            async def callback(_ctx) -> None:
                await self._call_worker(plugin, "invoke", {
                    "handler_id": handler_id,
                    "context": True,
                })

            return callback

        plugin.active_registration = ActiveRegistration(
            run=run,
            resources=resources,
            restart_policy=ActiveRestartPolicy(
                str(descriptor.get("restart_policy") or "on_failure")
            ),
            startup_timeout=float(descriptor.get("startup_timeout") or 20.0),
            shutdown_timeout=float(descriptor.get("shutdown_timeout") or 20.0),
            on_quiesce=lifecycle("on_quiesce"),
            on_resume=lifecycle("on_resume"),
            on_stop=lifecycle("on_stop"),
        )

    async def _resource_call(self, plugin, payload: dict[str, Any]) -> Any:
        resource = str(payload.get("resource") or "")
        operation = str(payload.get("operation") or "")
        args = list(payload.get("args") or [])
        kwargs = dict(payload.get("kwargs") or {})
        if resource == "runtime":
            runner = plugin.active_runner
            if runner is None:
                raise RuntimeError("Plugin active runtime is unavailable")
            control = runner.control
            method = getattr(control, operation, None)
            if method is None or operation.startswith("_"):
                raise PermissionError(f"Plugin runtime operation is unavailable: {operation}")
            value = method(*args, **kwargs)
            if asyncio.iscoroutine(value):
                value = await value
            return {"value": value, "state": control.safe_summary()}
        if resource == "process":
            return await self.processes.call(plugin, operation, args, kwargs)
        if resource == "workspace":
            return await self.workspaces.call(plugin, operation, args, kwargs)
        facade = self.manager.plugin_resource_facade(
            plugin,
            plugin.active_registration.resources,
        )
        port = getattr(facade, resource, None)
        if port is None:
            raise PermissionError(f"Plugin resource is unavailable: {resource}")
        method = getattr(port, operation, None)
        if method is None or operation.startswith("_"):
            raise PermissionError(f"Plugin resource operation is unavailable: {resource}.{operation}")
        value = method(*args, **kwargs)
        if asyncio.iscoroutine(value):
            value = await value
        outcome = getattr(value, "outcome", None)
        if callable(outcome):
            value = await outcome()
        return value

    def _worker_env(self, plugin) -> dict[str, str]:
        env = {
            "PATH": os.defpath,
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if os.name == "nt":
            for name in (
                "SystemDrive",
                "SystemRoot",
                "WINDIR",
                "COMSPEC",
                "PATHEXT",
            ):
                value = os.environ.get(name, "")
                if value:
                    env[name] = value
            data_root = Path(plugin.data_path or ".").resolve()
            app_data = data_root / ".windows-profile"
            local_data = app_data / "local"
            roaming_data = app_data / "roaming"
            for path in (app_data, local_data, roaming_data):
                path.mkdir(parents=True, exist_ok=True)
            env.update({
                "APPDATA": str(roaming_data),
                "HOME": str(data_root),
                "LOCALAPPDATA": str(local_data),
                "OS": "Windows_NT",
                "TEMP": str(data_root),
                "TMP": str(data_root),
                "USERPROFILE": str(data_root),
            })
        resolver = getattr(self.manager.settings, "get_env", None)
        for name in plugin.manifest.requires_env:
            value = resolver(name, "") if callable(resolver) else ""
            if value:
                env[name] = str(value)
        return env

    def _stage_tool_inputs(
        self,
        plugin,
        kwargs: dict[str, Any],
        bindings: tuple[ToolResourceBinding, ...],
    ) -> dict[str, Any]:
        if plugin.data_path is None:
            raise RuntimeError("Plugin data exchange directory is unavailable")
        exchange = Path(plugin.data_path) / ".exchange" / uuid4().hex
        exchange.mkdir(parents=True, exist_ok=True)
        rewritten = dict(kwargs)
        for binding in bindings:
            if binding.kind != "filesystem" or binding.access != "read":
                raise PermissionError(
                    f"Unsupported external plugin resource binding: {binding.kind}:{binding.access}"
                )
            raw = kwargs.get(binding.argument)
            if raw in (None, ""):
                continue
            requested = Path(str(raw)).expanduser()
            if requested.is_symlink():
                raise ValueError(f"External plugin input must not be a symbolic link: {requested}")
            if not requested.is_absolute():
                source = (Path.cwd() / requested).resolve()
            else:
                source = requested.resolve()
            if not source.is_file():
                raise ValueError(f"External plugin input is not a regular file: {source}")
            target = exchange / source.name
            shutil.copyfile(source, target)
            rewritten[binding.argument] = str(target.relative_to(Path(plugin.data_path)))
        return rewritten


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        fields = value.get("fields") if value.get("__type__") else None
        source = fields if isinstance(fields, dict) else value
        return {str(key): _plain(item) for key, item in source.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _active_resources(value: dict[str, Any]) -> ActiveResourceRequest:
    return ActiveResourceRequest(
        tools=tuple(value.get("tools") or ()),
        mcp={key: tuple(items) for key, items in dict(value.get("mcp") or {}).items()},
        required_mcp_servers=tuple(value.get("required_mcp_servers") or ()),
        optional_mcp_servers=tuple(value.get("optional_mcp_servers") or ()),
        llm=bool(value.get("llm", False)),
        conversation=bool(value.get("conversation", False)),
        delivery=bool(value.get("delivery", False)),
        events=bool(value.get("events", False)),
        artifacts=bool(value.get("artifacts", False)),
        processes=tuple(value.get("processes") or ()),
        workspaces=tuple(value.get("workspaces") or ()),
    )


def _invocation_metadata() -> dict[str, str]:
    try:
        from luna_agent.tools.runtime_context import current_tool_agent

        agent = current_tool_agent()
    except Exception:
        return {}
    security = getattr(agent, "_security_context", None)
    return {
        "session_key": str(
            getattr(security, "session_key", "")
            or getattr(agent, "_memory_session_key", "")
            or ""
        ),
        "operation_id": str(getattr(agent, "_hook_turn_id", "") or ""),
    }


class PluginHostProcessService:
    """Generation-owned interactive subprocesses selected from user configuration."""

    def __init__(self, manager) -> None:
        self.manager = manager
        self._processes: dict[str, dict[str, Any]] = {}

    def port(self, plugin):
        return BoundPluginProcessPort(self, plugin)

    async def call(
        self,
        plugin,
        operation: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> Any:
        if plugin.active_registration is None:
            raise PermissionError("Plugin process resource requires an active declaration")
        declared = set(plugin.active_registration.resources.processes)
        if operation == "start":
            name = str(kwargs.get("name") or (args[0] if args else ""))
            if name not in declared:
                raise PermissionError(f"Plugin process is not declared: {plugin.key}:{name}")
            return await self._start(plugin, name, kwargs)
        process_id = str(kwargs.get("process_id") or (args[0] if args else ""))
        record = self._record(plugin, process_id)
        process = record["process"]
        if operation == "write_line":
            if process.stdin is None:
                raise RuntimeError("Plugin process stdin is unavailable")
            value = str(kwargs.get("text") or (args[1] if len(args) > 1 else ""))
            process.stdin.write((value + "\n").encode("utf-8"))
            await process.stdin.drain()
            return {"written": True}
        if operation in {"read_line", "read_stderr_line"}:
            stream = process.stdout if operation == "read_line" else process.stderr
            if stream is None:
                return {"line": "", "eof": True}
            timeout = float(kwargs.get("timeout") or 0)
            reader = stream.readline()
            line = await asyncio.wait_for(reader, timeout=timeout) if timeout > 0 else await reader
            return {
                "line": line.decode("utf-8", errors="replace").rstrip("\r\n"),
                "eof": not bool(line),
                "returncode": process.returncode,
            }
        if operation == "status":
            return {"running": process.returncode is None, "returncode": process.returncode}
        if operation == "stop":
            await self._stop_record(process_id, record)
            return {"stopped": True}
        raise PermissionError(f"Plugin process operation is unavailable: {operation}")

    async def _start(self, plugin, name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        spec = self._spec(plugin, name)
        existing = [
            item for item in self._processes.values()
            if item["runtime_id"] == plugin.runtime_instance_id and item["name"] == name
        ]
        max_instances = max(1, int(spec.get("max_instances") or 1))
        if len(existing) >= max_instances:
            raise RuntimeError(f"Plugin process instance limit reached: {name}")
        executable = shutil.which(str(spec.get("executable") or ""))
        if not executable:
            raise FileNotFoundError(f"Configured plugin process executable was not found: {name}")
        prefix = [str(item) for item in spec.get("args_prefix", [])]
        extra = [str(item) for item in kwargs.get("args", [])]
        if extra and not bool(spec.get("allow_extra_args", False)):
            raise PermissionError(f"Plugin process does not allow extra arguments: {name}")
        cwd = Path(str(kwargs.get("cwd") or spec.get("cwd") or ".")).expanduser().resolve()
        roots = [Path(item).expanduser().resolve() for item in spec.get("cwd_roots", [])]
        if not roots or not any(cwd == root or root in cwd.parents for root in roots):
            raise PermissionError(f"Plugin process cwd is outside configured roots: {cwd}")
        requested_env = {str(k): str(v) for k, v in dict(kwargs.get("env") or {}).items()}
        allowed_env = set(str(item) for item in spec.get("env_allowlist", []))
        if set(requested_env) - allowed_env:
            raise PermissionError("Plugin process requested undeclared environment variables")
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            **{str(k): str(v) for k, v in dict(spec.get("env") or {}).items()},
            **requested_env,
        }
        seed_from = str(spec.get("seed_from") or "")
        seed_to = str(spec.get("seed_to") or "")
        if seed_from and seed_to:
            self._seed_directory(Path(seed_from).expanduser(), Path(seed_to).expanduser())
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = await asyncio.create_subprocess_exec(
            executable,
            *prefix,
            *extra,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
        process_id = uuid4().hex
        self._processes[process_id] = {
            "process": process,
            "runtime_id": plugin.runtime_instance_id,
            "name": name,
        }
        return {"process_id": process_id, "pid": process.pid, "name": name}

    def _spec(self, plugin, name: str) -> dict[str, Any]:
        all_config = getattr(self.manager.settings, "plugins_config", {}) or {}
        config = all_config.get(plugin.key, {}) if isinstance(all_config, dict) else {}
        if isinstance(config, dict) and plugin.key == "integrations/codex-bridge":
            config = self.manager.external_runtime.normalized_config(plugin, config)
        specs = config.get("host_processes", {}) if isinstance(config, dict) else {}
        spec = specs.get(name) if isinstance(specs, dict) else None
        # Explicit host_processes configuration wins over the Codex convenience
        # default so operators can tighten the executable, cwd, and environment.
        if name == "codex-app-server" and not isinstance(spec, dict) and isinstance(config, dict):
            spec = {
                "executable": config.get("command") or "codex",
                "args_prefix": ["app-server"],
                "cwd_roots": [config.get("development_root") or ".", config.get("cwd") or "."],
                "env": {"CODEX_HOME": str(config.get("runtime_codex_home") or "data/codex-bridge")},
                "seed_from": config.get("source_codex_home") or "",
                "seed_to": config.get("runtime_codex_home") or "",
                "max_instances": 8,
            }
        if not isinstance(spec, dict):
            raise PermissionError(f"Plugin host process is not configured: {plugin.key}:{name}")
        return spec

    @staticmethod
    def _seed_directory(source: Path, destination: Path) -> None:
        if not source.is_dir():
            return
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("auth.json", "config.toml"):
            source_file = source / name
            target = destination / name
            if source_file.is_file() and not target.exists():
                shutil.copyfile(source_file, target)
                try:
                    target.chmod(0o600)
                except OSError:
                    pass

    def _record(self, plugin, process_id: str) -> dict[str, Any]:
        record = self._processes.get(process_id)
        if record is None or record["runtime_id"] != plugin.runtime_instance_id:
            raise KeyError(f"Plugin process not found: {process_id}")
        return record

    def stop_generation(self, runtime_id: str) -> None:
        records = [
            (process_id, record)
            for process_id, record in self._processes.items()
            if record["runtime_id"] == runtime_id
        ]
        if not records:
            return

        async def stop_all() -> None:
            await asyncio.gather(
                *(self._stop_record(process_id, record) for process_id, record in records),
                return_exceptions=True,
            )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(stop_all())
        else:
            loop.create_task(stop_all(), name=f"plugin-process-stop:{runtime_id}")

    async def _stop_record(self, process_id: str, record: dict[str, Any]) -> None:
        self._processes.pop(process_id, None)
        process = record["process"]
        if process.returncode is not None:
            return
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            await process.wait()


class BoundPluginProcessPort:
    def __init__(self, service: PluginHostProcessService, plugin) -> None:
        self.service = service
        self.plugin = plugin

    async def start(self, *, name: str, **kwargs: Any) -> Any:
        return await self.service.call(
            self.plugin,
            "start",
            [],
            {"name": name, **kwargs},
        )

    async def write_line(self, *, process_id: str, text: str) -> Any:
        return await self.service.call(
            self.plugin, "write_line", [], {"process_id": process_id, "text": text}
        )

    async def read_line(self, *, process_id: str, timeout: float = 0) -> Any:
        return await self.service.call(
            self.plugin, "read_line", [], {"process_id": process_id, "timeout": timeout}
        )

    async def read_stderr_line(self, *, process_id: str, timeout: float = 0) -> Any:
        return await self.service.call(
            self.plugin,
            "read_stderr_line",
            [],
            {"process_id": process_id, "timeout": timeout},
        )

    async def status(self, *, process_id: str) -> Any:
        return await self.service.call(
            self.plugin, "status", [], {"process_id": process_id}
        )

    async def stop(self, *, process_id: str) -> Any:
        return await self.service.call(
            self.plugin, "stop", [], {"process_id": process_id}
        )


class PluginHostWorkspaceService:
    def __init__(self, manager) -> None:
        self.manager = manager

    def port(self, plugin):
        return BoundPluginWorkspacePort(self, plugin)

    async def call(self, plugin, operation: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        declared = tuple(plugin.active_registration.resources.workspaces)
        name = str(kwargs.get("name") or (args[0] if args else ""))
        if not name and len(declared) == 1:
            # Older external SDKs did not forward the bound workspace name.
            # A single declaration is unambiguous; multiple declarations must
            # continue to provide an explicit name.
            name = str(declared[0])
        if name not in declared:
            raise PermissionError(f"Plugin workspace is not declared: {plugin.key}:{name}")
        spec = self._spec(plugin, name)
        root_value = str(spec.get("root") or "").strip()
        if not root_value:
            raise PermissionError(f"Plugin workspace has no configured root: {name}")
        root = Path(root_value).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        if operation == "create":
            workspace = (root / str(kwargs.get("workspace") or kwargs.get("id") or "")).resolve()
            if root not in workspace.parents and workspace != root:
                raise PermissionError("Plugin workspace path escapes configured root")
            workspace.mkdir(parents=True, exist_ok=True)
            return {"path": str(workspace)}
        path = self._path(root, str(kwargs.get("path") or ""))
        if operation == "write":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(kwargs.get("text") or ""), encoding="utf-8")
            return {"path": str(path)}
        if operation == "read":
            return {"text": path.read_text(encoding="utf-8"), "path": str(path)}
        if operation == "copy":
            source = Path(str(kwargs.get("source") or "")).expanduser().resolve()
            read_roots = [
                Path(item).expanduser().resolve()
                for item in spec.get("read_roots", [])
                if str(item or "").strip()
            ]
            if not any(source == item or item in source.parents for item in read_roots):
                raise PermissionError("Plugin workspace source is outside configured read roots")
            if not source.is_file():
                raise FileNotFoundError(f"Plugin workspace source is not a file: {source}")
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, path)
            return {"path": str(path)}
        raise PermissionError(f"Plugin workspace operation is unavailable: {operation}")

    @staticmethod
    def _path(root: Path, relative: str) -> Path:
        candidate = Path(relative)
        path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        if path != root and root not in path.parents:
            raise PermissionError("Plugin workspace path escapes configured root")
        return path

    def _spec(self, plugin, name: str) -> dict[str, Any]:
        all_config = getattr(self.manager.settings, "plugins_config", {}) or {}
        config = all_config.get(plugin.key, {}) if isinstance(all_config, dict) else {}
        specs = config.get("host_workspaces", {}) if isinstance(config, dict) else {}
        if isinstance(specs, dict) and isinstance(specs.get(name), dict):
            return dict(specs[name])
        if name == "development" and isinstance(config, dict) and config.get("development_root"):
            configured_spec = str(config.get("development_spec_path") or "").strip()
            if configured_spec:
                spec_path = Path(configured_spec).expanduser()
            else:
                plugin_root = Path(plugin.manifest.path or "").resolve()
                spec_path = plugin_root.parents[1] / "docs" / "plugin-development.md"
            return {
                "root": config.get("development_root"),
                "read_roots": [str(spec_path)],
            }
        raise PermissionError(f"Plugin workspace is not configured: {plugin.key}:{name}")


class BoundPluginWorkspacePort:
    def __init__(self, service: PluginHostWorkspaceService, plugin) -> None:
        self.service = service
        self.plugin = plugin

    async def create(self, *, workspace: str) -> Any:
        return await self.service.call(self.plugin, "create", [], {"name": "development", "workspace": workspace})

    async def write(self, *, path: str, text: str) -> Any:
        return await self.service.call(self.plugin, "write", [], {"name": "development", "path": path, "text": text})

    async def read(self, *, path: str) -> Any:
        return await self.service.call(self.plugin, "read", [], {"name": "development", "path": path})

    async def copy(self, *, source: str, path: str) -> Any:
        return await self.service.call(self.plugin, "copy", [], {"name": "development", "source": source, "path": path})
