"""Lifecycle manager for independent MCP server runtimes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4
import inspect

from personal_agent.mcp.models import MCPServerConfig, MCPRuntimeState
from personal_agent.mcp.runtime import ConnectionFactory, MCPServerRuntime


class MCPManager:
    def __init__(
        self,
        server_configs: list[dict | MCPServerConfig],
        *,
        connection_factory: ConnectionFactory | None = None,
        reconnect_delays: tuple[float, ...] | None = None,
        env_values: Mapping[str, str] | None = None,
        process_backend: str = "legacy",
        sandbox_roots: list[Path] | None = None,
        work_dir: Path | None = None,
        on_tools_changed=None,
    ) -> None:
        configs = [
            item if isinstance(item, MCPServerConfig) else MCPServerConfig.from_mapping(item)
            for item in server_configs
        ]
        names = [config.name for config in configs]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"Duplicate MCP server name(s): {', '.join(duplicates)}")

        runtime_kwargs: dict[str, Any] = {}
        if connection_factory is not None:
            runtime_kwargs["connection_factory"] = connection_factory
        if reconnect_delays is not None:
            runtime_kwargs["reconnect_delays"] = reconnect_delays
        if env_values is not None:
            runtime_kwargs["env_values"] = env_values
        runtime_kwargs["process_backend"] = process_backend
        runtime_kwargs["sandbox_roots"] = list(sandbox_roots or [])
        runtime_kwargs["work_dir"] = work_dir
        runtime_kwargs["on_tools_changed"] = on_tools_changed
        self._runtime_kwargs = runtime_kwargs
        self._on_tools_changed = on_tools_changed
        self._runtimes = {config.name: self._new_runtime(config) for config in configs}
        self._retired_runtimes: dict[str, MCPServerRuntime] = {}
        self._running = False

    @property
    def total_tools(self) -> int:
        return sum(len(runtime.registered_names) for runtime in self._runtimes.values())

    @property
    def client_names(self) -> list[str]:
        return [name for name, runtime in self._runtimes.items() if runtime.ready]

    async def start(self) -> int:
        if self._running:
            return self.total_tools
        self._running = True
        await asyncio.gather(*(runtime.start() for runtime in self._runtimes.values()))
        return self.total_tools

    async def wait_initial_attempts(self) -> int:
        await asyncio.gather(
            *(runtime.wait_initial_attempt() for runtime in self._runtimes.values())
        )
        return self.total_tools

    async def stop(self) -> None:
        await asyncio.gather(
            *(runtime.stop() for runtime in [*self._runtimes.values(), *self._retired_runtimes.values()]),
            return_exceptions=True,
        )
        self._retired_runtimes.clear()
        self._running = False

    async def restart_server(self, name: str) -> int:
        runtime = self._runtimes.get(name)
        if runtime is None:
            raise KeyError(f"Unknown MCP server: {name}")
        replacement = await self.replace_server(runtime.config)
        return len(replacement.registered_names)

    async def reconcile(self, server_configs: list[dict | MCPServerConfig]) -> None:
        configs = {
            config.name: config
            for config in (
                item if isinstance(item, MCPServerConfig) else MCPServerConfig.from_mapping(item)
                for item in server_configs
            )
        }
        for name in sorted(set(self._runtimes) - set(configs)):
            await self.remove_server(name)
        for name, config in configs.items():
            current = self._runtimes.get(name)
            if current is None:
                await self.replace_server(config)
            elif current.config != config:
                await self.replace_server(config)

    async def replace_server(self, config: MCPServerConfig | dict) -> MCPServerRuntime:
        normalized = config if isinstance(config, MCPServerConfig) else MCPServerConfig.from_mapping(config)
        candidate = self._new_runtime(normalized, publish_tools=False)
        await candidate.start()
        await candidate.wait_initial_attempt()
        if candidate.state == MCPRuntimeState.FAILED:
            error = candidate.health_snapshot().get("last_error") or "MCP candidate failed"
            await candidate.stop()
            raise RuntimeError(str(error))

        previous = self._runtimes.get(normalized.name)
        if previous is not None:
            previous.deactivate_tools()
        self._runtimes[normalized.name] = candidate
        await candidate.activate_tools()
        if previous is not None:
            self._retired_runtimes[previous.runtime_instance_id] = previous
        return candidate

    async def remove_server(self, name: str) -> None:
        runtime = self._runtimes.pop(name, None)
        if runtime is None:
            return
        runtime.deactivate_tools()
        await self._notify_tools_changed(name, runtime.runtime_instance_id, set())
        self._retired_runtimes[runtime.runtime_instance_id] = runtime

    async def retire_runtime(self, runtime_instance_id: str) -> None:
        runtime = self._retired_runtimes.pop(runtime_instance_id, None)
        if runtime is not None:
            await runtime.stop()

    def health_snapshot(self) -> dict[str, Any]:
        servers = [runtime.health_snapshot() for runtime in self._runtimes.values()]
        enabled_runtimes = [
            runtime for runtime in self._runtimes.values() if runtime.config.enabled
        ]
        registered = sorted(
            name
            for runtime in self._runtimes.values()
            for name in runtime.registered_names
        )
        return {
            "running": self._running,
            "configured_count": len(self._runtimes),
            "enabled_count": len(enabled_runtimes),
            "initializing": self._running and any(
                not runtime.health_snapshot()["initial_attempt_done"]
                for runtime in enabled_runtimes
            ),
            "starting_count": sum(
                1 for runtime in self._runtimes.values()
                if runtime.state == MCPRuntimeState.CONNECTING
            ),
            "connected_count": sum(1 for runtime in self._runtimes.values() if runtime.ready),
            "degraded_count": sum(
                1 for runtime in self._runtimes.values()
                if runtime.state in {MCPRuntimeState.DEGRADED, MCPRuntimeState.RECONNECTING}
            ),
            "failed_count": sum(
                1 for runtime in self._runtimes.values()
                if runtime.state == MCPRuntimeState.FAILED
            ),
            "total_tools": len(registered),
            "retired_runtime_count": len(self._retired_runtimes),
            "registered_tools": registered,
            "servers": servers,
        }

    def _new_runtime(
        self,
        config: MCPServerConfig,
        *,
        publish_tools: bool = True,
    ) -> MCPServerRuntime:
        return MCPServerRuntime(
            config,
            runtime_instance_id=f"mcp:{config.name}:{uuid4().hex}",
            publish_tools=publish_tools,
            **self._runtime_kwargs,
        )

    async def _notify_tools_changed(
        self,
        server_name: str,
        runtime_instance_id: str,
        names: set[str],
    ) -> None:
        if self._on_tools_changed is None:
            return
        result = self._on_tools_changed(server_name, runtime_instance_id, names)
        if inspect.isawaitable(result):
            await result
