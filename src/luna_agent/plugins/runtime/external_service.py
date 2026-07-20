"""Central host adapter for out-of-process external plugin generations."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from luna_agent_plugin_sdk import (
    ActiveRegistration,
    ActiveResourceRequest,
    ActiveRestartPolicy,
    CommandEntry,
    ToolEntry,
)

from luna_agent.plugins.install import PluginEnvironment, PluginEnvironmentManager
from luna_agent.plugins.runtime.sandbox import build_plugin_worker_launch
from luna_agent.plugins.runtime.worker_client import PluginWorkerClient


class ExternalPluginRuntimeService:
    """Own environments, workers, host proxies, and resource RPC for external plugins."""

    def __init__(self, manager, root: Path) -> None:
        self.manager = manager
        self.environments = PluginEnvironmentManager(Path(root) / "environments")
        self.workers: dict[str, PluginWorkerClient] = {}

    def prepare_environment(self, plugin) -> PluginEnvironment:
        return self.environments.ensure(plugin.key, plugin.manifest.requires.python)

    def start(
        self,
        plugin,
        *,
        environment: PluginEnvironment,
        config: dict[str, Any],
    ) -> None:
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
            self.workers[plugin.runtime_instance_id] = worker
            plugin.worker = worker
            plugin.environment_id = environment.environment_id
            plugin.environment_path = environment.root
            plugin.sandbox_backend = launch.backend
            self._register_capabilities(plugin, dict(result.get("capabilities") or {}))
        except Exception as exc:
            detail = worker.last_stderr.strip()
            worker.stop()
            if detail:
                raise RuntimeError(
                    f"External plugin worker failed: {exc}; stderr: {detail[-8000:]}"
                ) from exc
            raise

    def stop(self, plugin) -> None:
        runtime_id = str(getattr(plugin, "runtime_instance_id", "") or "")
        worker = self.workers.pop(runtime_id, None) or getattr(plugin, "worker", None)
        if worker is not None:
            worker.stop()
        plugin.worker = None

    def summary(self, plugin) -> dict[str, Any]:
        worker = self.workers.get(plugin.runtime_instance_id)
        return {
            "isolated": worker is not None,
            "environment_id": str(getattr(plugin, "environment_id", "") or ""),
            "environment_path": str(getattr(plugin, "environment_path", "") or ""),
            "sandbox_backend": str(getattr(plugin, "sandbox_backend", "") or ""),
            "worker": worker.safe_summary() if worker is not None else {},
        }

    def _register_capabilities(self, plugin, capabilities: dict[str, Any]) -> None:
        ctx = plugin.ctx
        worker = plugin.worker
        if ctx is None or worker is None:
            raise RuntimeError(f"Plugin host proxy context is unavailable: {plugin.key}")
        for descriptor in capabilities.get("tools", []):
            value = _plain(descriptor)
            timeout = float(value.get("timeout_seconds") or 120.0)

            async def handler(
                _handler_id=value["handler_id"],
                _timeout=timeout,
                **kwargs,
            ):
                return await worker.call(
                    "invoke",
                    {"handler_id": _handler_id, "kwargs": kwargs},
                    timeout=_timeout,
                )

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
                return await worker.call("invoke", {
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
                return await worker.call("invoke", {
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
        worker = plugin.worker
        if worker is None:
            raise RuntimeError("Plugin worker is unavailable")
        resources = _active_resources(_plain(descriptor.get("resources") or {}))
        run_id = str(descriptor["run"])

        async def run(_ctx) -> None:
            await worker.call("active.start", {"handler_id": run_id})
            await worker.call("active.wait", {}, timeout=365 * 24 * 3600.0)

        def lifecycle(name: str):
            handler_id = str(descriptor.get(name) or "")
            if not handler_id:
                return None

            async def callback(_ctx) -> None:
                await worker.call("invoke", {
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
            raise PermissionError("Plugin process resource has not been declared")
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
        resolver = getattr(self.manager.settings, "get_env", None)
        for name in plugin.manifest.requires_env:
            value = resolver(name, "") if callable(resolver) else ""
            if value:
                env[name] = str(value)
        return env


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
    )
