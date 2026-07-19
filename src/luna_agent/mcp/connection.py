"""Official SDK-backed MCP connection adapters."""

from __future__ import annotations

import asyncio
import inspect
import json
import mimetypes
from pathlib import Path
import re
import shlex
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

import httpx
import mcp.types as mcp_types
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from luna_agent.mcp.models import (
    MCPCallResult,
    MCPContentBlock,
    MCPServerConfig,
    MCPServerInfo,
    MCPToolSpec,
    MCPTransport,
)
from luna_agent.tools.env_filter import filter_env

NotificationCallback = Callable[[str], Awaitable[None] | None]
HTTPClientFactory = Callable[[dict[str, str], float, float], httpx.AsyncClient]

# npx/npm launchers must keep stdout reserved for JSON-RPC. These defaults only
# silence launcher chatter; an explicitly configured server environment wins.
_NPM_STDIO_ENV_DEFAULTS = {
    "npm_config_loglevel": "silent",
    "npm_config_update_notifier": "false",
    "NO_UPDATE_NOTIFIER": "1",
    "NPM_CONFIG_FUND": "false",
    "NPM_CONFIG_AUDIT": "false",
}


class MCPConnection(Protocol):
    async def connect(self) -> MCPServerInfo: ...

    async def list_tools(self) -> list[MCPToolSpec]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPCallResult: ...

    async def ping(self) -> None: ...

    async def close(self) -> None: ...


class SDKMCPConnection:
    """Own an SDK transport and session behind Luna Agent's stable contract."""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        notification_callback: NotificationCallback | None = None,
        http_client_factory: HTTPClientFactory | None = None,
        env_values: Mapping[str, str] | None = None,
        process_backend: str = "legacy",
        sandbox_roots: list[Path] | None = None,
        work_dir: Path | None = None,
    ) -> None:
        self.config = config
        self._notification_callback = notification_callback
        self._http_client_factory = http_client_factory or _default_http_client
        self._env_values = dict(env_values or {})
        self._process_backend = process_backend
        self._sandbox_roots = list(sandbox_roots or [])
        self._work_dir = Path(work_dir).resolve() if work_dir is not None else Path.cwd()
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._connect_lock = asyncio.Lock()
        self._stderr_file = None
        self._stderr_history: list[str] = []
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
            params = self._stdio_parameters()
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(params, errlog=self._stderr_file)
            )
        else:
            headers = _http_headers(self.config.headers_env, self._env_values)
            _validate_http_target(self.config)
            http_client = self._http_client_factory(
                headers,
                self.config.connect_timeout_seconds,
                self.config.call_timeout_seconds,
            )
            await stack.enter_async_context(http_client)
            read_stream, write_stream, _ = await stack.enter_async_context(
                streamable_http_client(
                    self.config.url,
                    http_client=http_client,
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

    def _stdio_parameters(self) -> StdioServerParameters:
        from luna_agent.tools.process_sandbox import build_process_launch

        command = shlex.join([self.config.command, *self.config.args])
        launch = build_process_launch(
            command,
            cwd=self._work_dir,
            writable_roots=self._sandbox_roots,
            allow_network=self.config.allow_network,
            requested_backend=self._process_backend,
        )
        if launch.backend == "unavailable":
            raise ValueError(launch.warning)
        env = _stdio_env(self.config.env)
        if launch.backend == "bwrap":
            env["HOME"] = str(self._work_dir)
            env["TMPDIR"] = "/tmp"
            return StdioServerParameters(
                command=launch.argv[0],
                args=list(launch.argv[1:]),
                env=env,
                cwd=launch.cwd,
            )
        return StdioServerParameters(
            command=self.config.command,
            args=list(self.config.args),
            env=env,
            cwd=launch.cwd,
        )

    async def list_tools(self) -> list[MCPToolSpec]:
        session = self._require_session()
        cursor: str | None = None
        tools: list[MCPToolSpec] = []
        pages = 0
        while True:
            pages += 1
            if pages > self.config.max_tool_pages:
                raise ValueError(
                    f"MCP tool pagination exceeded {self.config.max_tool_pages} pages"
                )
            result = await session.list_tools(cursor=cursor)
            for tool in result.tools:
                schema = dict(tool.inputSchema or {})
                schema_size = len(
                    json.dumps(schema, ensure_ascii=False, default=str).encode("utf-8")
                )
                if schema_size > self.config.max_schema_bytes:
                    raise ValueError(
                        f"MCP tool schema exceeds {self.config.max_schema_bytes} bytes: {tool.name}"
                    )
                tools.append(
                    MCPToolSpec(
                        name=str(tool.name),
                        description=_truncate_text(str(tool.description or ""), 8000),
                        input_schema=schema,
                    )
                )
                if len(tools) > self.config.max_tools:
                    raise ValueError(
                        f"MCP tool count exceeds configured limit {self.config.max_tools}"
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
        return _normalize_call_result(result, self.config, work_dir=self._work_dir)

    async def ping(self) -> None:
        await self._require_session().send_ping()

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
            self._stderr_history = _read_stderr_tail(stderr_file)
            stderr_file.close()

    def stderr_tail(self, limit: int = 20) -> list[str]:
        stream = self._stderr_file
        if stream is None:
            return self._stderr_history[-limit:]
        return _read_stderr_tail(stream, limit=limit)

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
    for key, value in _NPM_STDIO_ENV_DEFAULTS.items():
        result.setdefault(key, value)
    result.update(configured)
    return result


def _read_stderr_tail(stream, *, limit: int = 20) -> list[str]:
    try:
        stream.flush()
        position = stream.tell()
        stream.seek(0)
        lines = stream.read().splitlines()
        stream.seek(position)
        return lines[-limit:]
    except (OSError, ValueError):
        return []


def _http_headers(headers_env: dict[str, str], env_values: Mapping[str, str]) -> dict[str, str]:
    missing = sorted(env_name for env_name in headers_env.values() if not env_values.get(env_name))
    if missing:
        raise ValueError(f"MCP header environment variable is not set: {', '.join(missing)}")
    return {header: env_values[env_name] for header, env_name in headers_env.items()}


def _default_http_client(headers: dict[str, str], connect_timeout: float, call_timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(call_timeout, connect=connect_timeout),
        follow_redirects=False,
    )


def _validate_http_target(config: MCPServerConfig) -> None:
    from urllib.parse import urlparse

    from luna_agent.tools.url_safety import check_url

    parsed = urlparse(config.url)
    if parsed.scheme == "http" and not config.allow_insecure_http:
        raise ValueError(
            "MCP HTTP transport requires HTTPS; set allow_insecure_http: true "
            "for an explicitly trusted endpoint"
        )
    error = check_url(config.url, allow_private=config.allow_private_network)
    if error:
        raise ValueError(f"Unsafe MCP HTTP endpoint: {error.removeprefix('Error: ')}")


def _normalize_call_result(
    result,
    config: MCPServerConfig,
    *,
    work_dir: Path | None = None,
) -> MCPCallResult:
    blocks: list[MCPContentBlock] = []
    text_parts: list[str] = []
    remaining_text = config.max_result_chars
    for item in list(result.content)[:64]:
        raw = item.model_dump(by_alias=True, exclude_none=True)
        block_type = str(raw.get("type") or "unknown")
        if block_type == "text":
            text = _truncate_text(str(raw.get("text") or ""), remaining_text)
            remaining_text = max(0, remaining_text - len(text))
            text_parts.append(text)
            blocks.append(MCPContentBlock(type=block_type, text=text))
            blocks.extend(_linked_artifact_blocks(text, config, work_dir=work_dir))
        elif block_type in {"image", "audio"}:
            mime_type = str(raw.get("mimeType") or "")
            data = str(raw.get("data") or "")
            data, truncated = _bounded_artifact(data, config.max_artifact_bytes)
            text_parts.append(f"[{block_type}: {mime_type or 'unknown'}]")
            blocks.append(
                MCPContentBlock(
                    type=block_type,
                    mime_type=mime_type,
                    data=data,
                    metadata={"truncated": True} if truncated else {},
                )
            )
        elif block_type == "resource":
            resource = raw.get("resource") if isinstance(raw.get("resource"), dict) else {}
            uri = str(resource.get("uri") or "")
            mime_type = str(resource.get("mimeType") or "")
            text = _truncate_text(str(resource.get("text") or ""), remaining_text)
            remaining_text = max(0, remaining_text - len(text))
            text_parts.append(text or f"[resource: {uri or 'unknown'}]")
            data = str(resource.get("blob") or "")
            data, truncated = _bounded_artifact(data, config.max_artifact_bytes)
            blocks.append(
                MCPContentBlock(
                    type=block_type,
                    text=text,
                    mime_type=mime_type,
                    data=data,
                    uri=_truncate_text(uri, 2048),
                    metadata={"truncated": True} if truncated else {},
                )
            )
        else:
            blocks.append(MCPContentBlock(type=block_type, metadata={"omitted": True}))

    structured = getattr(result, "structuredContent", None)
    bounded_structured, structured_truncated = _bounded_structured(
        structured,
        config.max_result_chars,
    )
    if not text_parts and bounded_structured is not None:
        text_parts.append(json.dumps(bounded_structured, ensure_ascii=False))
    return MCPCallResult(
        text=_truncate_text(
            "\n".join(part for part in text_parts if part),
            config.max_result_chars,
        ),
        content=blocks,
        is_error=bool(result.isError),
        metadata=(
            {
                "structured_content": bounded_structured,
                "structured_content_truncated": structured_truncated,
            }
            if structured is not None
            else {}
        ),
    )


_MARKDOWN_LINK_RE = re.compile(r"\[[^\]\n]*\]\(([^)\n]+)\)")


def _linked_artifact_blocks(
    text: str,
    config: MCPServerConfig,
    *,
    work_dir: Path | None,
) -> list[MCPContentBlock]:
    """Promote files linked by an explicitly trusted local-output MCP server."""
    if not config.artifact_roots or work_dir is None:
        return []

    base = Path(work_dir).resolve()
    roots = [
        (Path(value).expanduser() if Path(value).expanduser().is_absolute() else base / value).resolve()
        for value in config.artifact_roots
        if str(value).strip()
    ]
    allowed_extensions = {value.lower() for value in config.artifact_extensions}
    blocks: list[MCPContentBlock] = []
    seen: set[Path] = set()
    for match in _MARKDOWN_LINK_RE.finditer(text):
        target = match.group(1).strip().strip("<>")
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        relative = Path(unquote(parsed.path))
        if relative.is_absolute():
            continue
        for root in roots:
            unresolved = root / relative
            if unresolved.is_symlink():
                continue
            candidate = unresolved.resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate in seen or not candidate.is_file() or candidate.is_symlink():
                continue
            if allowed_extensions and candidate.suffix.lower() not in allowed_extensions:
                continue
            seen.add(candidate)
            size = candidate.stat().st_size
            mime_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            blocks.append(MCPContentBlock(
                type="resource",
                mime_type=mime_type,
                uri=candidate.as_uri(),
                metadata={
                    "filename": candidate.name,
                    "truncated": size > config.max_artifact_bytes,
                },
            ))
            break
    return blocks


def _truncate_text(value: str, limit: int) -> str:
    maximum = max(0, int(limit))
    if len(value) <= maximum:
        return value
    marker = "\n...[truncated by MCP safety limit]"
    if maximum <= len(marker):
        return value[:maximum]
    return value[: maximum - len(marker)] + marker


def _bounded_artifact(data: str, max_bytes: int) -> tuple[str, bool]:
    estimated_bytes = len(data.encode("utf-8")) * 3 // 4
    if estimated_bytes <= max_bytes:
        return data, False
    return "", True


def _bounded_structured(value: Any, max_chars: int) -> tuple[Any, bool]:
    if value is None:
        return None, False
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded) <= max_chars:
        return value, False
    return {"truncated": True, "preview": _truncate_text(encoded, max_chars)}, True
