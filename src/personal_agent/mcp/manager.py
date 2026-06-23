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

    async def start(self) -> int:
        """Connect all enabled MCP servers concurrently. Returns total tools registered."""
        if not self._server_configs:
            return 0

        logger.info("Connecting %d MCP server(s)...", len(self._server_configs))

        # Connect all servers concurrently
        async def _connect_one(cfg: MCPServerConfig) -> tuple[MCPServerConfig, MCPClient | None]:
            client = MCPClient(cfg)
            try:
                tools = await client.connect()
            except Exception:
                logger.exception("MCP server '%s': unexpected error during connect", cfg.name)
                tools = []
            if tools:
                return cfg, client
            else:
                # Connection failed or no tools — clean up
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return cfg, None

        tasks = [_connect_one(cfg) for cfg in self._server_configs]
        results = await _gather_with_grace(*tasks)

        # Register tools from successfully connected servers
        from personal_agent.tools.entry import ToolEntry
        from personal_agent.tools.registry import tool_registry

        count = 0
        for cfg, client in results:
            if client is None or not client.connected:
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
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
                count += 1

        self._total_tools = count
        logger.info("MCP: %d tools registered from %d server(s)",
                      count, len(self._clients))
        return count

    async def stop(self) -> None:
        """Disconnect all MCP servers."""
        for name, client in self._clients.items():
            try:
                await client.disconnect()
            except Exception:
                logger.exception("MCP server '%s': error during disconnect", name)

        from personal_agent.tools.registry import tool_registry

        # Unregister all MCP tools
        prefixes = [f"{MCP_PREFIX}{name}__" for name in self._clients.keys()]
        to_remove = [
            name for name in tool_registry.all_names
            if any(name.startswith(p) for p in prefixes)
        ]
        for name in to_remove:
            tool_registry.unregister(name)

        logger.debug("MCP: unregistered %d tools", len(to_remove))
        self._clients.clear()
        self._total_tools = 0


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
