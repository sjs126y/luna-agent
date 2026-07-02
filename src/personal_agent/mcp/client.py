"""MCPClient — single MCP server connection via stdio subprocess + JSON-RPC.

Hand-rolled JSON-RPC 2.0 — no external MCP SDK dependency.
Protocol: https://spec.modelcontextprotocol.io/specification/
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"
JSONRPC_VERSION = "2.0"
MCP_STDERR_TAIL_LINES = 20
MCP_STDERR_LINE_LIMIT = 4000


@dataclass
class MCPToolInfo:
    name: str
    description: str
    inputSchema: dict


@dataclass
class MCPServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


class MCPClient:
    """Manages one MCP server: spawn → handshake → tool discovery → call → shutdown."""

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=MCP_STDERR_TAIL_LINES)
        self._request_id: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()
        self._tools: list[MCPToolInfo] = []
        self._server_name: str = ""
        self._server_version: str = ""
        self._connected: bool = False
        self._last_error: str = ""
        self._last_call_error: str = ""
        self._last_connected_at: str = ""
        self._last_disconnected_at: str = ""

    # ── public API ──────────────────────────────────────

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def tools(self) -> list[MCPToolInfo]:
        return list(self._tools)

    @property
    def connected(self) -> bool:
        return self._connected

    def health_snapshot(self) -> dict[str, Any]:
        """Return a bounded, JSON-safe snapshot for diagnostics."""
        proc = self._process
        return {
            "name": self._config.name,
            "command": self._config.command,
            "args": list(self._config.args),
            "enabled": bool(self._config.enabled),
            "connected": bool(self._connected),
            "pid": proc.pid if proc is not None else None,
            "tool_count": len(self._tools),
            "server_name": self._server_name,
            "server_version": self._server_version,
            "last_error": self._last_error,
            "last_call_error": self._last_call_error,
            "last_connected_at": self._last_connected_at,
            "last_disconnected_at": self._last_disconnected_at,
            "stderr_tail": list(self._stderr_tail),
        }

    async def connect(self) -> list[MCPToolInfo]:
        """Spawn subprocess → initialize handshake → list tools. Returns tool list."""
        if self._connected:
            return list(self._tools)

        # ── resolve command path ──
        import shutil
        resolved = shutil.which(self._config.command)
        if resolved is None:
            message = f"command not found: {self._config.command}"
            self._last_error = message
            logger.warning("MCP server '%s': %s", self._config.name, message)
            return []
        command = resolved

        # ── spawn subprocess ──
        from personal_agent.tools.env_filter import filter_env
        safe_keys = {"PATH", "HOME", "USER", "USERNAME", "TMP", "TMPDIR", "TEMP",
                     "SYSTEMROOT", "SYSTEMDRIVE", "APPDATA", "LOCALAPPDATA",
                     "PATHEXT", "COMSPEC", "ProgramFiles", "ProgramFiles(x86)"}
        env = filter_env()  # strip credentials first
        filtered_env = {k: v for k, v in env.items() if k in safe_keys or k in self._config.env}
        filtered_env.update(self._config.env)

        try:
            self._process = await asyncio.create_subprocess_exec(
                command,
                *self._config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=filtered_env,
            )
            self._stderr_task = asyncio.create_task(self._drain_stderr())
        except FileNotFoundError:
            message = f"command not found: {self._config.command}"
            self._last_error = message
            logger.warning("MCP server '%s': %s", self._config.name, message)
            return []
        except Exception as exc:
            self._last_error = f"spawn failed: {type(exc).__name__}: {exc}"
            logger.exception("MCP server '%s': failed to spawn", self._config.name)
            return []

        # ── initialize handshake ──
        try:
            init_result = await asyncio.wait_for(
                self._initialize(), timeout=15.0
            )
        except asyncio.TimeoutError:
            self._last_error = "initialize timed out"
            logger.warning("MCP server '%s': initialize timed out", self._config.name)
            await self.disconnect()
            return []
        except Exception as exc:
            self._last_error = f"initialize failed: {type(exc).__name__}: {exc}"
            logger.exception("MCP server '%s': initialize failed", self._config.name)
            await self.disconnect()
            return []

        self._server_name = init_result.get("serverInfo", {}).get("name", self._config.name)
        self._server_version = init_result.get("serverInfo", {}).get("version", "")

        # ── list tools ──
        try:
            tools_result = await asyncio.wait_for(
                self._list_tools(), timeout=10.0
            )
        except asyncio.TimeoutError:
            self._last_error = "list_tools timed out"
            logger.warning("MCP server '%s': list_tools timed out", self._config.name)
            await self.disconnect()
            return []
        except Exception as exc:
            self._last_error = f"list_tools failed: {type(exc).__name__}: {exc}"
            logger.exception("MCP server '%s': list_tools failed", self._config.name)
            await self.disconnect()
            return []

        self._tools = [
            MCPToolInfo(
                name=t.get("name", ""),
                description=t.get("description", ""),
                inputSchema=t.get("inputSchema", {"type": "object", "properties": {}}),
            )
            for t in tools_result.get("tools", [])
        ]
        self._connected = True
        self._last_error = ""
        self._last_call_error = ""
        self._last_connected_at = _utc_now()

        logger.info("MCP server '%s' connected: %d tools (%s %s)",
                     self._config.name, len(self._tools),
                     self._server_name, self._server_version)
        return list(self._tools)

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call a tool on the MCP server. Returns the text result."""
        if not self._connected or self._process is None:
            self._last_call_error = "MCP server not connected"
            return "Error: MCP server not connected"

        try:
            async with self._lock:
                result = await asyncio.wait_for(
                    self._call_tool_raw(tool_name, arguments),
                    timeout=120.0,
                )
        except asyncio.TimeoutError:
            self._last_call_error = f"tool call timed out: {tool_name}"
            await self.disconnect()
            raise
        except (BrokenPipeError, ConnectionError) as exc:
            self._last_call_error = f"{type(exc).__name__}: {exc}"
            await self.disconnect()
            raise
        except Exception as exc:
            self._last_call_error = f"{type(exc).__name__}: {exc}"
            raise

        # Extract text from content blocks
        content = result.get("content", [])
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif isinstance(block, dict) and block.get("type") == "image":
                # Image data — include a note but not the base64 blob
                texts.append(f"[image: {block.get('mimeType', 'unknown')}]")
            elif isinstance(block, str):
                texts.append(block)

        if not texts:
            return json.dumps(result, ensure_ascii=False)

        return "\n".join(texts)

    async def disconnect(self) -> None:
        """Gracefully terminate the subprocess, draining all pipes."""
        self._connected = False
        self._last_disconnected_at = _utc_now()
        proc = self._process
        self._process = None
        self._tools.clear()
        await self._stop_stderr_task()
        if proc is None:
            return
        # communicate() drains stdout+stderr and closes all pipes cleanly
        # — avoids "I/O operation on closed pipe" on Windows ProactorEventLoop
        try:
            proc.terminate()
            await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
        except Exception:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass

    async def _drain_stderr(self) -> None:
        """Continuously drain stderr and keep only recent lines for diagnostics."""
        proc = self._process
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if len(text) > MCP_STDERR_LINE_LIMIT:
                    text = text[:MCP_STDERR_LINE_LIMIT] + "..."
                if text:
                    self._stderr_tail.append(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("MCP '%s': stderr drain failed", self._config.name, exc_info=True)

    async def _stop_stderr_task(self) -> None:
        task = self._stderr_task
        self._stderr_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # ── JSON-RPC internals ──────────────────────────────

    async def _initialize(self) -> dict:
        """MCP initialize handshake."""
        result = await self._rpc_call("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {
                "name": "personal-agent",
                "version": "1.0.0",
            },
        })
        # Send initialized notification (no response expected)
        await self._rpc_notify("notifications/initialized")
        return result

    async def _list_tools(self) -> dict:
        """Call tools/list to discover server tools."""
        return await self._rpc_call("tools/list", {})

    async def _call_tool_raw(self, tool_name: str, arguments: dict) -> dict:
        """Call tools/call — raw result extraction."""
        return await self._rpc_call("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })

    async def _rpc_call(self, method: str, params: dict) -> dict:
        """Send a JSON-RPC request and wait for the response.

        NOTE: caller must hold self._lock to prevent interleaved writes on stdin.
        """
        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("MCP process not running")

        self._request_id += 1
        request_id = self._request_id

        request = {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "method": method,
            "params": params,
        }
        request_line = json.dumps(request, ensure_ascii=False) + "\n"

        self._process.stdin.write(request_line.encode("utf-8"))
        await self._process.stdin.drain()

        while True:
            line = await self._process.stdout.readline()
            if not line:
                raise ConnectionError("MCP server stdout closed")

            try:
                response = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                logger.debug("MCP '%s': non-JSON line from server: %s",
                             self._config.name, line[:200])
                continue

            if response.get("id") == request_id:
                if "error" in response:
                    err = response["error"]
                    raise RuntimeError(
                        f"MCP error {err.get('code', '?')}: {err.get('message', 'unknown')}"
                    )
                return response.get("result", {})

    async def _rpc_notify(self, method: str, params: dict | None = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if self._process is None or self._process.stdin is None:
            return

        notification = {
            "jsonrpc": JSONRPC_VERSION,
            "method": method,
            "params": params or {},
        }
        notification_line = json.dumps(notification, ensure_ascii=False) + "\n"

        async with self._lock:
            self._process.stdin.write(notification_line.encode("utf-8"))
            await self._process.stdin.drain()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
