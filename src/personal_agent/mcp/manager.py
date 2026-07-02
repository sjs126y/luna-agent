"""MCPManager — lifecycle manager for multiple MCP servers.

Startup: read config → connect each server → wrap tools as ToolEntry → register.
MCP tools are NOT core → deferrable → discoverable via tool_search bridge.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from personal_agent.mcp.client import MCPClient, MCPServerConfig

if TYPE_CHECKING:
    from personal_agent.tools.entry import ToolEntry

logger = logging.getLogger(__name__)

# Tool name prefix to avoid collisions with built-in tools
MCP_PREFIX = "mcp__"


class MCPManager:
    """Manages the lifecycle of all MCP server connections."""

    def __init__(self, server_configs: list[dict]) -> None:
        self._clients: dict[str, MCPClient] = {}
        self._server_clients: dict[str, MCPClient] = {}
        self._registered_tool_names: set[str] = set()
        self._running: bool = False
        self._server_configs = [
            MCPServerConfig(
                name=cfg.get("name", cfg.get("command", "unknown")),
                command=cfg.get("command", ""),
                args=cfg.get("args", []),
                env=cfg.get("env", {}),
                enabled=cfg.get("enabled", True),
            )
            for cfg in server_configs
            if cfg.get("enabled", True) and cfg.get("command", "")
        ]
        self._total_tools: int = 0

    # ── public API ──────────────────────────────────────

    @property
    def total_tools(self) -> int:
        return self._total_tools

    @property
    def client_names(self) -> list[str]:
        return list(self._clients.keys())

    def health_snapshot(self) -> dict:
        servers = []
        for cfg in self._server_configs:
            client = self._server_clients.get(cfg.name)
            if client is not None:
                servers.append(client.health_snapshot())
            else:
                servers.append({
                    "name": cfg.name,
                    "command": cfg.command,
                    "args": list(cfg.args),
                    "enabled": bool(cfg.enabled),
                    "connected": False,
                    "pid": None,
                    "tool_count": 0,
                    "server_name": "",
                    "server_version": "",
                    "last_error": "",
                    "last_call_error": "",
                    "last_connected_at": "",
                    "last_disconnected_at": "",
                    "stderr_tail": [],
                })
        return {
            "running": self._running,
            "configured_count": len(self._server_configs),
            "connected_count": sum(1 for item in servers if item.get("connected")),
            "total_tools": self._total_tools,
            "registered_tools": sorted(self._registered_tool_names),
            "servers": servers,
        }

    async def start(self) -> int:
        """Connect all enabled MCP servers concurrently. Returns total tools registered."""
        if not self._server_configs:
            self._running = True
            return 0

        logger.info("Connecting %d MCP server(s)...", len(self._server_configs))
        self._running = True

        # Connect all servers concurrently
        async def _connect_one(cfg: MCPServerConfig) -> tuple[MCPServerConfig, MCPClient]:
            client = MCPClient(cfg)
            try:
                tools = await client.connect()
            except Exception:
                logger.exception("MCP server '%s': unexpected error during connect", cfg.name)
                tools = []
            if not tools and not client.connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            return cfg, client

        tasks = [_connect_one(cfg) for cfg in self._server_configs]
        results = await _gather_with_grace(*tasks)

        # Register tools from successfully connected servers
        from personal_agent.tools.entry import ToolEntry
        from personal_agent.tools.registry import tool_registry

        count = 0
        for result in results:
            if result is None:
                continue
            cfg, client = result
            self._server_clients[cfg.name] = client
            if not client.connected:
                continue

            self._clients[cfg.name] = client
            tool_prefix = f"{MCP_PREFIX}{cfg.name}__"

            for tool in client.tools:
                entry = ToolEntry(
                    name=f"{tool_prefix}{tool.name}",
                    description=f"[MCP {cfg.name}] {tool.description}",
                    schema=tool.inputSchema if tool.inputSchema else {
                        "type": "object",
                        "properties": {},
                    },
                    handler=_make_mcp_handler(client, tool.name),
                    toolset="mcp",
                    is_parallel_safe=True,
                    is_destructive=False,
                )
                tool_registry.register(entry)
                self._registered_tool_names.add(entry.name)
                count += 1

        self._total_tools = count
        logger.info("MCP: %d tools registered from %d server(s)",
                      count, len(self._clients))
        return count

    async def stop(self) -> None:
        """Disconnect all MCP servers."""
        for name, client in self._server_clients.items():
            try:
                await client.disconnect()
            except Exception:
                logger.exception("MCP server '%s': error during disconnect", name)

        from personal_agent.tools.registry import tool_registry

        to_remove = sorted(self._registered_tool_names)
        for name in to_remove:
            tool_registry.unregister(name)

        logger.debug("MCP: unregistered %d tools", len(to_remove))
        self._clients.clear()
        self._server_clients.clear()
        self._registered_tool_names.clear()
        self._total_tools = 0
        self._running = False


# ── helpers ────────────────────────────────────────────

def _make_mcp_handler(client: MCPClient, tool_name: str):
    """Create an async handler closure that calls the MCP client."""
    async def handler(**kwargs):
        return await client.call_tool(tool_name, kwargs)
    return handler


async def _gather_with_grace(*coros):
    """Gather coroutines, returning (result|None) for each. Never raises."""
    import asyncio
    results = await asyncio.gather(*coros, return_exceptions=True)
    out = []
    for r in results:
        if isinstance(r, BaseException):
            out.append(None)
        else:
            out.append(r)
    return out
