"""Official SDK-backed MCP connection adapters."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, Protocol

import mcp.types as mcp_types
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from personal_agent.mcp.models import (
    MCPCallResult,
    MCPContentBlock,
    MCPServerConfig,
    MCPServerInfo,
    MCPToolSpec,
    MCPTransport,
)
from personal_agent.tools.env_filter import filter_env

NotificationCallback = Callable[[str], Awaitable[None] | None]


class MCPConnection(Protocol):
    async def connect(self) -> MCPServerInfo: ...

    async def list_tools(self) -> list[MCPToolSpec]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPCallResult: ...

    async def close(self) -> None: ...


class SDKMCPConnection:
    """Own an SDK transport and session behind Lumora's stable contract."""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        notification_callback: NotificationCallback | None = None,
    ) -> None:
        self.config = config
        self._notification_callback = notification_callback
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._connect_lock = asyncio.Lock()
        self._stderr_file = None
        self._server_info: MCPServerInfo | None = None

    @property
    def connected(self) -> bool:
        return self._session is not None

    @property
    def server_info(self) -> MCPServerInfo | None:
        return self._server_info

    async def connect(self) -> MCPServerInfo:
        async with self._connect_lock:
            if self._server_info is not None and self._session is not None:
                return self._server_info
            try:
                return await asyncio.wait_for(
                    self._connect_inner(),
                    timeout=self.config.connect_timeout_seconds,
                )
            except BaseException:
                await self._close_unlocked()
                raise

    async def _connect_inner(self) -> MCPServerInfo:
        stack = AsyncExitStack()
        self._stack = stack
        if self.config.transport == MCPTransport.STDIO:
            self._stderr_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
            params = StdioServerParameters(
                command=self.config.command,
                args=list(self.config.args),
                env=_stdio_env(self.config.env),
            )
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(params, errlog=self._stderr_file)
            )
        else:
            read_stream, write_stream, _ = await stack.enter_async_context(
                streamablehttp_client(
                    self.config.url,
                    headers=_http_headers(self.config.headers_env),
                    timeout=self.config.connect_timeout_seconds,
                )
            )

        session = ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=self.config.call_timeout_seconds),
            message_handler=self._handle_message,
        )
        self._session = await stack.enter_async_context(session)
        initialized = await self._session.initialize()
        server = initialized.serverInfo
        self._server_info = MCPServerInfo(
            name=str(server.name or self.config.name),
            version=str(server.version or ""),
            protocol_version=str(initialized.protocolVersion or ""),
            capabilities=initialized.capabilities.model_dump(by_alias=True, exclude_none=True),
        )
        return self._server_info

    async def list_tools(self) -> list[MCPToolSpec]:
        session = self._require_session()
        cursor: str | None = None
        tools: list[MCPToolSpec] = []
        while True:
            result = await session.list_tools(cursor=cursor)
            tools.extend(
                MCPToolSpec(
                    name=str(tool.name),
                    description=str(tool.description or ""),
                    input_schema=dict(tool.inputSchema or {}),
                )
                for tool in result.tools
            )
            cursor = result.nextCursor
            if not cursor:
                return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPCallResult:
        session = self._require_session()
        result = await session.call_tool(
            name,
            arguments,
            read_timeout_seconds=timedelta(seconds=self.config.call_timeout_seconds),
        )
        return _normalize_call_result(result)

    async def close(self) -> None:
        async with self._connect_lock:
            await self._close_unlocked()

    async def _close_unlocked(self) -> None:
        stack = self._stack
        self._stack = None
        self._session = None
        self._server_info = None
        if stack is not None:
            await stack.aclose()
        stderr_file = self._stderr_file
        self._stderr_file = None
        if stderr_file is not None:
            stderr_file.close()

    def stderr_tail(self, limit: int = 20) -> list[str]:
        stream = self._stderr_file
        if stream is None:
            return []
        try:
            stream.flush()
            position = stream.tell()
            stream.seek(0)
            lines = stream.read().splitlines()
            stream.seek(position)
            return lines[-limit:]
        except (OSError, ValueError):
            return []

    async def _handle_message(self, message) -> None:
        if isinstance(message, mcp_types.ToolListChangedNotification):
            callback = self._notification_callback
            if callback is not None:
                value = callback("tools/list_changed")
                if inspect.isawaitable(value):
                    await value

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise ConnectionError(f"MCP server not connected: {self.config.name}")
        return self._session


def _stdio_env(configured: dict[str, str]) -> dict[str, str]:
    safe_keys = {
        "PATH", "HOME", "USER", "USERNAME", "TMP", "TMPDIR", "TEMP",
        "SYSTEMROOT", "SYSTEMDRIVE", "APPDATA", "LOCALAPPDATA", "PATHEXT",
        "COMSPEC", "ProgramFiles", "ProgramFiles(x86)",
    }
    base = filter_env()
    result = {key: value for key, value in base.items() if key in safe_keys or key in configured}
    result.update(configured)
    return result


def _http_headers(headers_env: dict[str, str]) -> dict[str, str]:
    return {
        header: os.environ[env_name]
        for header, env_name in headers_env.items()
        if env_name in os.environ
    }


def _normalize_call_result(result) -> MCPCallResult:
    blocks: list[MCPContentBlock] = []
    text_parts: list[str] = []
    for item in result.content:
        raw = item.model_dump(by_alias=True, exclude_none=True)
        block_type = str(raw.get("type") or "unknown")
        if block_type == "text":
            text = str(raw.get("text") or "")
            text_parts.append(text)
            blocks.append(MCPContentBlock(type=block_type, text=text))
        elif block_type in {"image", "audio"}:
            mime_type = str(raw.get("mimeType") or "")
            data = str(raw.get("data") or "")
            text_parts.append(f"[{block_type}: {mime_type or 'unknown'}]")
            blocks.append(MCPContentBlock(type=block_type, mime_type=mime_type, data=data))
        elif block_type == "resource":
            resource = raw.get("resource") if isinstance(raw.get("resource"), dict) else {}
            uri = str(resource.get("uri") or "")
            mime_type = str(resource.get("mimeType") or "")
            text = str(resource.get("text") or "")
            text_parts.append(text or f"[resource: {uri or 'unknown'}]")
            blocks.append(MCPContentBlock(type=block_type, text=text, mime_type=mime_type, uri=uri))
        else:
            blocks.append(MCPContentBlock(type=block_type, metadata=raw))

    structured = getattr(result, "structuredContent", None)
    if not text_parts and structured is not None:
        text_parts.append(json.dumps(structured, ensure_ascii=False))
    return MCPCallResult(
        text="\n".join(part for part in text_parts if part),
        content=blocks,
        is_error=bool(result.isError),
        metadata={"structured_content": structured} if structured is not None else {},
    )
