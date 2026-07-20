"""External plugin worker bootstrap."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import sys
from pathlib import Path
from types import MappingProxyType
from typing import Any

from luna_agent_plugin_sdk.active import ActiveResourceRequest
from luna_agent_plugin_sdk.manifest import CommandEntry
from luna_agent_plugin_sdk.tools import ToolEntry
from luna_agent_plugin_sdk.version import SDK_VERSION
from luna_agent_plugin_sdk.worker_protocol import (
    FramedRPCPeer,
    PROTOCOL_VERSION,
    to_wire,
)


class WorkerStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative_path: str | Path) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise ValueError("plugin storage path must be relative")
        resolved = (self.root / candidate).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise ValueError("plugin storage path escapes isolated root")
        return resolved

    def read_text(self, relative_path: str | Path, *, default: str = "") -> str:
        try:
            return self.resolve(relative_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return default

    def write_text(self, relative_path: str | Path, text: str) -> Path:
        path = self.resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(text), encoding="utf-8")
        return path

    def exists(self, relative_path: str | Path) -> bool:
        return self.resolve(relative_path).exists()

    def read_json(
        self,
        relative_path: str | Path,
        *,
        default: Any = None,
        schema_version: int | None = None,
    ) -> Any:
        try:
            value = json.loads(self.resolve(relative_path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        if schema_version is not None:
            if not isinstance(value, dict) or int(value.get("schema_version") or 0) != schema_version:
                raise ValueError(f"plugin storage schema mismatch: {relative_path}")
        return value

    def write_json_atomic(self, relative_path: str | Path, value: Any) -> Path:
        path = self.resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(temporary, path)
        return path


class WorkerTaskPort:
    def __init__(self) -> None:
        self.tasks: set[asyncio.Task[Any]] = set()

    def create(self, awaitable, *, name: str = "") -> asyncio.Task[Any]:
        task = asyncio.create_task(awaitable, name=name or "plugin-worker-task")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def close(self) -> None:
        for task in tuple(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


class RemoteResourceNamespace:
    def __init__(self, peer: FramedRPCPeer, resource: str) -> None:
        self.peer = peer
        self.resource = resource

    def __getattr__(self, operation: str):
        async def call(*args, **kwargs):
            return await self.peer.call("resource.call", {
                "resource": self.resource,
                "operation": operation,
                "args": list(args),
                "kwargs": kwargs,
            })

        return call


class RemoteResources:
    def __init__(self, peer: FramedRPCPeer, storage: WorkerStorage) -> None:
        self.storage = storage
        for name in ("tool", "mcp", "llm", "conversation", "delivery", "events", "artifacts", "process"):
            setattr(self, name, RemoteResourceNamespace(peer, name))


class WorkerRegistrationPort:
    def __init__(self, runtime: "PluginWorkerRuntime") -> None:
        self.runtime = runtime

    def tool(self, entry: ToolEntry) -> None:
        unsupported = [
            name for name in (
                "check_fn", "availability_reason_fn", "precheck",
                "approval_mode_resolver", "resource_resolver", "timeout_resolver",
            )
            if getattr(entry, name, None) is not None
        ]
        if unsupported:
            raise ValueError(
                "External plugin tool uses unsupported host callbacks: " + ", ".join(unsupported)
            )
        handler_id = self.runtime.bind("tool", entry.name, entry.handler)
        self.runtime.capabilities["tools"].append({
            "handler_id": handler_id,
            "name": entry.name,
            "description": entry.description,
            "schema": entry.schema,
            "toolset": entry.toolset,
            "permission_category": entry.permission_category,
            "tags": list(entry.tags),
            "risk_level": entry.risk_level,
            "usage_hint": entry.usage_hint,
            "approval_mode": entry.approval_mode,
            "idempotent": entry.idempotent,
            "is_parallel_safe": entry.is_parallel_safe,
            "is_destructive": entry.is_destructive,
            "report_as_tool": entry.report_as_tool,
            "timeout_seconds": entry.timeout_seconds,
        })

    def skill(self, entry: Any) -> None:
        self.runtime.capabilities["skills"].append(to_wire(entry))

    def skills(self, relative_path: str | Path = "skills") -> int:
        path = self.runtime.context.resolve_path(relative_path)
        if not path.is_dir():
            raise ValueError(f"Plugin skills directory does not exist: {relative_path}")
        count = sum(1 for item in path.rglob("SKILL.md") if item.is_file())
        self.runtime.capabilities["skill_directories"].append(str(relative_path))
        return count

    def workflow(self, definition: Any) -> None:
        self.runtime.capabilities["workflows"].append(to_wire(definition))

    def platform(self, _entry: Any) -> None:
        raise ValueError("External plugins cannot register platform adapters")

    def mcp_server(self, config: Any) -> None:
        self.runtime.capabilities["mcp_servers"].append(to_wire(config))

    def mcp(self, relative_path: str | Path = "mcp.yaml") -> int:
        path = self.runtime.context.resolve_path(relative_path)
        if not path.is_file():
            raise ValueError(f"Plugin MCP configuration does not exist: {relative_path}")
        self.runtime.capabilities["mcp_files"].append(str(relative_path))
        return 1

    def hook(self, event: Any, callback: Any, priority: int = 100, **kwargs: Any) -> None:
        event_name = str(getattr(event, "value", event))
        handler_id = self.runtime.bind("hook", str(kwargs.get("name") or event_name), callback)
        self.runtime.capabilities["hooks"].append({
            "handler_id": handler_id,
            "event": event_name,
            "priority": int(priority),
            "name": str(kwargs.get("name") or ""),
            "matcher": str(kwargs.get("matcher") or "*"),
            "timeout": kwargs.get("timeout"),
        })

    def command(self, entry: CommandEntry) -> None:
        handler_id = self.runtime.bind("command", entry.name, entry.handler)
        self.runtime.capabilities["commands"].append({
            "handler_id": handler_id,
            "name": entry.name,
            "description": entry.description,
            "scope": entry.scope,
        })

    def memory_provider(self, **_kwargs: Any) -> None:
        raise ValueError("External plugins cannot register memory providers")

    def active(
        self,
        *,
        run: Any,
        resources: Any = None,
        restart_policy: str = "on_failure",
        startup_timeout: float = 20.0,
        shutdown_timeout: float = 20.0,
        on_quiesce: Any = None,
        on_resume: Any = None,
        on_stop: Any = None,
    ) -> None:
        if self.runtime.capabilities["active"]:
            raise ValueError("External plugin can register only one active runner")
        callbacks = {
            "run": self.runtime.bind("active", "run", run),
            "on_quiesce": self.runtime.bind("active", "on_quiesce", on_quiesce) if on_quiesce else "",
            "on_resume": self.runtime.bind("active", "on_resume", on_resume) if on_resume else "",
            "on_stop": self.runtime.bind("active", "on_stop", on_stop) if on_stop else "",
        }
        self.runtime.capabilities["active"].append({
            **callbacks,
            "resources": to_wire(resources or ActiveResourceRequest()),
            "restart_policy": str(getattr(restart_policy, "value", restart_policy)),
            "startup_timeout": float(startup_timeout),
            "shutdown_timeout": float(shutdown_timeout),
        })


class WorkerPluginContext:
    def __init__(
        self,
        runtime: "PluginWorkerRuntime",
        *,
        plugin_key: str,
        generation_id: str,
        runtime_instance_id: str,
        root: Path,
        data_root: Path,
        config: dict[str, Any],
    ) -> None:
        self.plugin_key = plugin_key
        self.generation_id = generation_id
        self.runtime_instance_id = runtime_instance_id
        self.root = root.resolve()
        self.config = MappingProxyType(dict(config))
        self.storage = WorkerStorage(data_root)
        self.tasks = WorkerTaskPort()
        self.resources = RemoteResources(runtime.peer, self.storage)
        self.register = WorkerRegistrationPort(runtime)
        self.runtime: Any = None

    def parse_config(self, model_type: Any) -> Any:
        validator = getattr(model_type, "model_validate", None)
        if not callable(validator):
            raise TypeError("Plugin config model must provide model_validate()")
        return validator(dict(self.config))

    def get_env(self, name: str, default: str = "") -> str:
        return str(os.environ.get(str(name), default) or default)

    def resolve_path(self, relative_path: str | Path) -> Path:
        candidate = Path(relative_path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        candidate = candidate.resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError(f"Plugin path escapes package root: {relative_path}")
        return candidate


class PluginWorkerRuntime:
    def __init__(self, peer: FramedRPCPeer) -> None:
        self.peer = peer
        self.context: WorkerPluginContext | None = None
        self.handlers: dict[str, Any] = {}
        self.capabilities: dict[str, list[Any]] = {
            "tools": [],
            "skills": [],
            "skill_directories": [],
            "workflows": [],
            "mcp_servers": [],
            "mcp_files": [],
            "hooks": [],
            "commands": [],
            "active": [],
        }
        self.active_task: asyncio.Task[Any] | None = None
        peer.register("initialize", self.initialize)
        peer.register("invoke", self.invoke)
        peer.register("shutdown", self.shutdown)
        peer.register("health", self.health)

    def bind(self, kind: str, name: str, handler: Any) -> str:
        if not callable(handler):
            raise TypeError(f"Plugin {kind} handler is not callable: {name}")
        handler_id = f"{kind}:{name}:{len(self.handlers) + 1}"
        self.handlers[handler_id] = handler
        return handler_id

    async def initialize(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.context is not None:
            raise RuntimeError("Plugin worker is already initialized")
        root = Path(str(payload["plugin_root"])).resolve()
        self.context = WorkerPluginContext(
            self,
            plugin_key=str(payload["plugin_key"]),
            generation_id=str(payload["generation_id"]),
            runtime_instance_id=str(payload["runtime_instance_id"]),
            root=root,
            data_root=Path(str(payload["data_root"])),
            config=dict(payload.get("config") or {}),
        )
        module_name, _, function_name = str(payload["entrypoint"]).partition(":")
        function_name = function_name or "register"
        sys.path.insert(0, str(root))
        try:
            module = importlib.import_module(module_name)
            register = getattr(module, function_name, None)
            if not callable(register):
                raise TypeError(f"Plugin entrypoint is not callable: {payload['entrypoint']}")
            result = register(self.context)
            if inspect.isawaitable(result):
                raise TypeError("Plugin register() must be synchronous")
        finally:
            try:
                sys.path.remove(str(root))
            except ValueError:
                pass
        return {
            "protocol_version": PROTOCOL_VERSION,
            "sdk_version": SDK_VERSION,
            "capabilities": self.capabilities,
        }

    async def invoke(self, payload: dict[str, Any]) -> Any:
        handler_id = str(payload.get("handler_id") or "")
        handler = self.handlers.get(handler_id)
        if handler is None:
            raise KeyError(f"Plugin handler not found: {handler_id}")
        args = list(payload.get("args") or [])
        kwargs = dict(payload.get("kwargs") or {})
        result = handler(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def health(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "ready": self.context is not None,
            "pid": os.getpid(),
            "handlers": len(self.handlers),
            "active_running": self.active_task is not None and not self.active_task.done(),
        }

    async def shutdown(self, _payload: dict[str, Any]) -> dict[str, Any]:
        if self.active_task is not None:
            self.active_task.cancel()
            await asyncio.gather(self.active_task, return_exceptions=True)
        if self.context is not None:
            await self.context.tasks.close()
        asyncio.get_running_loop().call_later(0.05, lambda: asyncio.create_task(self.peer.close()))
        return {"stopped": True}


async def _run() -> None:
    protocol_fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = os.fdopen(os.dup(sys.stderr.fileno()), "w", buffering=1, encoding="utf-8")
    protocol_writer = os.fdopen(protocol_fd, "wb", buffering=0)
    peer = FramedRPCPeer(sys.stdin.buffer, protocol_writer)
    PluginWorkerRuntime(peer)
    await peer.start()
    while not peer.closed:
        await asyncio.sleep(0.05)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
