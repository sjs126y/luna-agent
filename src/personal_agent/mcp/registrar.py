"""Synchronize one MCP server's tool snapshot with Lumora's ToolRegistry."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from personal_agent.mcp.models import MCPCallResult, MCPToolSpec
from personal_agent.security.models import ResourceRequirement
from personal_agent.tools.entry import ToolArtifact, ToolEntry, ToolHandlerOutput
from personal_agent.tools.registry import tool_registry

MCP_PREFIX = "mcp__"
ToolCaller = Callable[[str, dict], Awaitable[MCPCallResult]]


class MCPToolRegistrar:
    def __init__(
        self,
        server_name: str,
        call_tool: ToolCaller,
        *,
        server_url: str = "",
        call_timeout_seconds: float = 120.0,
        availability_reason: Callable[[], str] | None = None,
        publish_tools: bool = True,
    ) -> None:
        self.server_name = server_name
        self._call_tool = call_tool
        self._network_requirement = _network_requirement(server_url, server_name)
        self._call_timeout_seconds = float(call_timeout_seconds)
        self._availability_reason = availability_reason
        self._registered: dict[str, str] = {}
        self._entries: dict[str, ToolEntry] = {}
        self._available = False
        self._global_active = bool(publish_tools)

    @property
    def registered_names(self) -> set[str]:
        return set(self._registered)

    @property
    def global_active(self) -> bool:
        return self._global_active

    def sync(self, tools: list[MCPToolSpec]) -> bool:
        changed = False
        desired = {self._local_name(tool.name): tool for tool in tools}
        removed = set(self._registered) - set(desired)
        for name in removed:
            if self._global_active and tool_registry.get(name) is self._entries.get(name):
                tool_registry.unregister(name)
            self._registered.pop(name, None)
            self._entries.pop(name, None)
            changed = True

        for local_name, spec in desired.items():
            fingerprint = _tool_fingerprint(spec)
            if self._registered.get(local_name) == fingerprint:
                continue
            existing = tool_registry.get(local_name)
            if self._global_active and existing is not None and local_name not in self._registered:
                raise RuntimeError(f"MCP tool name collision: {local_name}")
            entry = self._entry(local_name, spec)
            self._entries[local_name] = entry
            if self._global_active:
                tool_registry.register(entry)
            self._registered[local_name] = fingerprint
            changed = True
        return changed

    def activate_global(self) -> bool:
        if self._global_active:
            return False
        for name, entry in self._entries.items():
            existing = tool_registry.get(name)
            if existing is not None and existing is not entry:
                raise RuntimeError(f"MCP tool name collision: {name}")
        self._global_active = True
        for entry in self._entries.values():
            tool_registry.register(entry)
        return bool(self._entries)

    def deactivate_global(self) -> bool:
        if not self._global_active:
            return False
        for name, entry in self._entries.items():
            if tool_registry.get(name) is entry:
                tool_registry.unregister(name)
        self._global_active = False
        return bool(self._entries)

    def set_available(self, available: bool) -> None:
        value = bool(available)
        if self._available == value:
            return
        self._available = value
        if self._registered and self._global_active:
            tool_registry.invalidate()

    def unregister_all(self) -> bool:
        changed = bool(self._registered)
        for name in sorted(self._registered):
            if self._global_active and tool_registry.get(name) is self._entries.get(name):
                tool_registry.unregister(name)
        self._registered.clear()
        self._entries.clear()
        self._available = False
        return changed

    def _entry(self, local_name: str, spec: MCPToolSpec) -> ToolEntry:
        async def handler(**kwargs):
            result = await self._call_tool(spec.name, kwargs)
            artifacts = [
                ToolArtifact(
                    kind=block.type,
                    name=str(block.metadata.get("filename") or ""),
                    mime_type=block.mime_type,
                    data=block.data,
                    uri=block.uri,
                    metadata=dict(block.metadata),
                )
                for block in result.content
                if block.type in {"image", "audio", "resource"}
            ]
            return ToolHandlerOutput(
                text=result.text,
                artifacts=artifacts,
                metadata={
                    "mcp_server": self.server_name,
                    "remote_tool": spec.name,
                    "structured_content": result.metadata.get("structured_content"),
                },
                is_error=result.is_error,
            )

        return ToolEntry(
            name=local_name,
            description=f"[MCP {self.server_name}] {spec.description}",
            schema=spec.input_schema or {"type": "object", "properties": {}},
            handler=handler,
            toolset="mcp",
            check_fn=lambda: self._available,
            availability_reason_fn=self._availability_reason,
            approval_mode="cached",
            resource_resolver=(
                (lambda _input: [self._network_requirement])
                if self._network_requirement is not None
                else None
            ),
            idempotent=False,
            is_parallel_safe=False,
            is_destructive=False,
            timeout_seconds=self._call_timeout_seconds,
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


def _network_requirement(url: str, server_name: str) -> ResourceRequirement | None:
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return ResourceRequirement(
        "network",
        f"{parsed.scheme}://{parsed.hostname}:{port}",
        "connect",
        f"MCP server {server_name}",
    )
