"""Lifecycle manager for independent MCP server runtimes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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
        self._runtimes = {
            config.name: MCPServerRuntime(config, **runtime_kwargs)
            for config in configs
        }
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
            *(runtime.stop() for runtime in self._runtimes.values()),
            return_exceptions=True,
        )
        self._running = False

    async def restart_server(self, name: str) -> int:
        runtime = self._runtimes.get(name)
        if runtime is None:
            raise KeyError(f"Unknown MCP server: {name}")
        return await runtime.restart()

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
            "registered_tools": registered,
            "servers": servers,
        }
