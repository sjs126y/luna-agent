"""Backward-compatible MCP client facade backed by the official SDK."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from personal_agent.mcp.connection import SDKMCPConnection
from personal_agent.mcp.models import MCPServerConfig, MCPToolSpec


@dataclass(frozen=True)
class MCPToolInfo:
    name: str
    description: str
    inputSchema: dict

    @classmethod
    def from_spec(cls, spec: MCPToolSpec) -> "MCPToolInfo":
        return cls(spec.name, spec.description, dict(spec.input_schema))


class MCPClient:
    """Compatibility surface for callers that used the original stdio client."""

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._connection = SDKMCPConnection(config)
        self._tools: list[MCPToolInfo] = []
        self._server_name = ""
        self._server_version = ""
        self._protocol_version = ""
        self._last_error = ""
        self._last_call_error = ""
        self._last_connected_at = ""
        self._last_disconnected_at = ""
        self._process = None

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def tools(self) -> list[MCPToolInfo]:
        return list(self._tools)

    @property
    def connected(self) -> bool:
        return self._connection.connected

    async def connect(self) -> list[MCPToolInfo]:
        if self.connected:
            return list(self._tools)
        try:
            info = await self._connection.connect()
            self._tools = [MCPToolInfo.from_spec(item) for item in await self._connection.list_tools()]
        except Exception as exc:
            self._last_error = _compat_error(exc, self._config)
            await self._connection.close()
            return []
        self._server_name = info.name
        self._server_version = info.version
        self._protocol_version = info.protocol_version
        self._last_error = ""
        self._last_call_error = ""
        self._last_connected_at = _now()
        return list(self._tools)

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        if not self.connected:
            self._last_call_error = "MCP server not connected"
            return "Error: MCP server not connected"
        try:
            result = await self._connection.call_tool(tool_name, arguments)
        except Exception as exc:
            self._last_call_error = _compat_error(exc, self._config)
            raise RuntimeError(self._last_call_error) from exc
        self._last_call_error = result.text if result.is_error else ""
        if result.is_error:
            return f"Error: {result.text or 'MCP tool call failed'}"
        return result.text

    async def disconnect(self) -> None:
        was_connected = self.connected
        await self._connection.close()
        self._tools.clear()
        if was_connected:
            self._last_disconnected_at = _now()

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "name": self._config.name,
            "transport": self._config.transport.value,
            "command": self._config.command,
            "args": list(self._config.args),
            "url": self._config.url,
            "enabled": bool(self._config.enabled),
            "connected": self.connected,
            "pid": None,
            "tool_count": len(self._tools),
            "server_name": self._server_name,
            "server_version": self._server_version,
            "protocol_version": self._protocol_version,
            "last_error": self._last_error,
            "last_call_error": self._last_call_error,
            "last_connected_at": self._last_connected_at,
            "last_disconnected_at": self._last_disconnected_at,
            "stderr_tail": self._connection.stderr_tail(),
        }


def _compat_error(exc: BaseException, config: MCPServerConfig) -> str:
    if isinstance(exc, FileNotFoundError):
        return f"command not found: {config.command}"
    return f"{type(exc).__name__}: {exc}"


def _now() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%S")
