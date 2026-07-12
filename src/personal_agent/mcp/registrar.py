"""Synchronize one MCP server's tool snapshot with Lumora's ToolRegistry."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from personal_agent.mcp.models import MCPCallResult, MCPToolSpec
from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry

MCP_PREFIX = "mcp__"
ToolCaller = Callable[[str, dict], Awaitable[MCPCallResult]]


class MCPToolRegistrar:
    def __init__(self, server_name: str, call_tool: ToolCaller) -> None:
        self.server_name = server_name
        self._call_tool = call_tool
        self._registered: dict[str, str] = {}
        self._available = False

    @property
    def registered_names(self) -> set[str]:
        return set(self._registered)

    def sync(self, tools: list[MCPToolSpec]) -> None:
        desired = {self._local_name(tool.name): tool for tool in tools}
        removed = set(self._registered) - set(desired)
        for name in removed:
            tool_registry.unregister(name)
            self._registered.pop(name, None)

        for local_name, spec in desired.items():
            fingerprint = _tool_fingerprint(spec)
            if self._registered.get(local_name) == fingerprint:
                continue
            existing = tool_registry.get(local_name)
            if existing is not None and local_name not in self._registered:
                raise RuntimeError(f"MCP tool name collision: {local_name}")
            tool_registry.register(self._entry(local_name, spec))
            self._registered[local_name] = fingerprint

    def set_available(self, available: bool) -> None:
        value = bool(available)
        if self._available == value:
            return
        self._available = value
        if self._registered:
            tool_registry.invalidate()

    def unregister_all(self) -> None:
        for name in sorted(self._registered):
            tool_registry.unregister(name)
        self._registered.clear()
        self._available = False

    def _entry(self, local_name: str, spec: MCPToolSpec) -> ToolEntry:
        async def handler(**kwargs):
            result = await self._call_tool(spec.name, kwargs)
            if result.is_error:
                return f"Error: {result.text or 'MCP tool call failed'}"
            return result.text

        return ToolEntry(
            name=local_name,
            description=f"[MCP {self.server_name}] {spec.description}",
            schema=spec.input_schema or {"type": "object", "properties": {}},
            handler=handler,
            toolset="mcp",
            check_fn=lambda: self._available,
            is_parallel_safe=True,
            is_destructive=False,
        )

    def _local_name(self, remote_name: str) -> str:
        return f"{MCP_PREFIX}{self.server_name}__{remote_name}"


def _tool_fingerprint(tool: MCPToolSpec) -> str:
    return json.dumps(
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
