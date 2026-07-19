"""Long-running lifecycle for one configured MCP server."""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Mapping

from luna_agent.mcp.connection import MCPConnection, SDKMCPConnection
from luna_agent.mcp.models import MCPCallResult, MCPRuntimeState, MCPServerConfig, MCPServerInfo
from luna_agent.mcp.registrar import MCPToolRegistrar

logger = logging.getLogger(__name__)

DEFAULT_RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
# The SDK stdio transport closes stdin, waits up to two seconds, and then
# terminates the process tree with a second two-second budget. Leave room for
# task-group and platform scheduling overhead before interrupting that cleanup.
MCP_STOP_TIMEOUT_SECONDS = 10.0
MCP_STOP_CANCELLATION_TIMEOUT_SECONDS = 2.0
ConnectionFactory = Callable[[MCPServerConfig, Callable[[str], Any]], MCPConnection]


class MCPServerRuntime:
    def __init__(
        self,
        config: MCPServerConfig,
        *,
        connection_factory: ConnectionFactory | None = None,
        reconnect_delays: tuple[float, ...] = DEFAULT_RECONNECT_DELAYS,
        health_interval_seconds: float = 30.0,
        refresh_debounce_seconds: float = 0.1,
        jitter: Callable[[], float] | None = None,
        env_values: Mapping[str, str] | None = None,
        process_backend: str = "legacy",
        sandbox_roots: list[Path] | None = None,
        work_dir: Path | None = None,
        runtime_instance_id: str = "",
        on_tools_changed: Callable[[str, str, set[str]], Any] | None = None,
        publish_tools: bool = True,
    ) -> None:
        self.config = config
        self.state = MCPRuntimeState.DISABLED if not config.enabled else MCPRuntimeState.STOPPED
        self._env_values = dict(env_values or {})
        self._process_backend = process_backend
        self._sandbox_roots = list(sandbox_roots or [])
        base_work_dir = Path(work_dir).resolve() if work_dir is not None else Path.cwd()
        self._work_dir = _resolve_work_dir(base_work_dir, config.work_dir)
        self.runtime_instance_id = runtime_instance_id or f"mcp:{config.name}"
        self._on_tools_changed = on_tools_changed
        self._connection_factory = connection_factory or self._create_connection
        self._reconnect_delays = reconnect_delays or DEFAULT_RECONNECT_DELAYS
        self._health_interval = health_interval_seconds
        self._refresh_debounce = refresh_debounce_seconds
        self._jitter = jitter or (lambda: random.uniform(0, 0.25))
        self._connection: MCPConnection | None = None
        self._owner_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._reconnect_event = asyncio.Event()
        self._refresh_event = asyncio.Event()
        self._initial_attempt_done = asyncio.Event()
        self._registrar = MCPToolRegistrar(
            config.name,
            self.call_tool,
            server_url=config.url,
            call_timeout_seconds=config.call_timeout_seconds,
            availability_reason=self.availability_reason,
            publish_tools=publish_tools,
        )
        self._server_info: MCPServerInfo | None = None
        self._last_error = ""
        self._last_call_error = ""
        self._last_connected_at = ""
        self._last_disconnected_at = ""
        self._next_retry_at = 0.0
        self._connection_attempts = 0
        self._reconnect_attempts = 0
        self._tool_refresh_count = 0
        self._notification_count = 0
        self._stderr_tail: list[str] = []
        self._initial_attempt_started_at = 0.0
        self._initial_attempt_duration_seconds = 0.0
        self._last_shutdown_error = ""
        self._shutdown_timeout_count = 0

    @property
    def ready(self) -> bool:
        return self.state in {MCPRuntimeState.READY, MCPRuntimeState.DEGRADED} and self._connection is not None

    @property
    def registered_names(self) -> set[str]:
        return self._registrar.registered_names

    def availability_reason(self) -> str:
        label = "starting" if self.state == MCPRuntimeState.CONNECTING else self.state.value
        detail = self._last_error or self._last_call_error
        message = f"MCP server '{self.config.name}' is {label}"
        return f"{message}: {detail}" if detail else message

    async def activate_tools(self) -> None:
        if self._registrar.activate_global():
            await self._notify_tools_changed()

    def deactivate_tools(self) -> None:
        self._registrar.deactivate_global()

    async def start(self) -> int:
        if not self.config.enabled:
            self.state = MCPRuntimeState.DISABLED
            return 0
        if self._owner_task is not None and not self._owner_task.done():
            return len(self.registered_names)
        self._work_dir.mkdir(parents=True, exist_ok=True)
        self._reset_events()
        self.state = MCPRuntimeState.CONNECTING
        self._initial_attempt_started_at = time.monotonic()
        self._initial_attempt_duration_seconds = 0.0
        self._owner_task = asyncio.create_task(self._run(), name=f"mcp-runtime:{self.config.name}")
        return len(self.registered_names)

    async def wait_initial_attempt(self) -> int:
        if not self.config.enabled:
            return 0
        if self._owner_task is None:
            await self.start()
        await self._initial_attempt_done.wait()
        return len(self.registered_names)

    async def stop(self) -> None:
        task = self._owner_task
        if task is None:
            was_active = self._registrar.global_active
            changed = self._registrar.unregister_all()
            if changed and was_active:
                await self._notify_tools_changed()
            self.state = MCPRuntimeState.DISABLED if not self.config.enabled else MCPRuntimeState.STOPPED
            return
        was_connecting = self.state in {
            MCPRuntimeState.CONNECTING,
            MCPRuntimeState.RECONNECTING,
        }
        self.state = MCPRuntimeState.STOPPING
        self._registrar.set_available(False)
        self._stop_event.set()
        self._reconnect_event.set()
        self._last_shutdown_error = ""
        if was_connecting:
            # An in-flight connect has no stop-event protocol. Cancel it so
            # shutdown does not wait for the configured connect timeout.
            task.cancel()
        try:
            timeout = (
                MCP_STOP_CANCELLATION_TIMEOUT_SECONDS
                if was_connecting
                else MCP_STOP_TIMEOUT_SECONDS
            )
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            self._shutdown_timeout_count += 1
            self._last_shutdown_error = (
                f"graceful shutdown exceeded {MCP_STOP_TIMEOUT_SECONDS:g}s; "
                "cancelling MCP runtime task"
            )
            logger.warning("Timed out stopping MCP server '%s'; cancelling runtime task", self.config.name)
            task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=MCP_STOP_CANCELLATION_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                self._last_shutdown_error = (
                    f"{self._last_shutdown_error}; cancellation did not finish within "
                    f"{MCP_STOP_CANCELLATION_TIMEOUT_SECONDS:g}s"
                )
                logger.error(
                    "MCP server '%s' did not finish cancellation cleanup", self.config.name
                )
        finally:
            self._mark_initial_attempt_done()
            self._owner_task = None
            was_active = self._registrar.global_active
            changed = self._registrar.unregister_all()
            if changed and was_active:
                await self._notify_tools_changed()
            self.state = MCPRuntimeState.STOPPED

    async def restart(self) -> int:
        await self.stop()
        await self.start()
        return await self.wait_initial_attempt()

    async def call_tool(self, name: str, arguments: dict) -> MCPCallResult:
        connection = self._connection
        if not self.ready or connection is None:
            self._reconnect_event.set()
            return _unavailable_result(self.config.name)
        try:
            result = await connection.call_tool(name, arguments)
            self._last_call_error = "" if not result.is_error else result.text
            return result
        except asyncio.CancelledError:
            self._last_call_error = "MCP tool call cancelled; reconnecting transport"
            self._last_error = self._last_call_error
            self.state = MCPRuntimeState.RECONNECTING
            self._registrar.set_available(False)
            self._reconnect_event.set()
            raise
        except Exception as exc:
            self._last_call_error = _error_text(exc, self.config)
            self._last_error = self._last_call_error
            self.state = MCPRuntimeState.RECONNECTING
            self._registrar.set_available(False)
            self._reconnect_event.set()
            return _unavailable_result(self.config.name, self._last_call_error)

    def health_snapshot(self) -> dict[str, Any]:
        info = self._server_info
        connection = self._connection
        stderr_tail = (
            connection.stderr_tail()
            if isinstance(connection, SDKMCPConnection)
            else self._stderr_tail
        )
        return {
            "name": self.config.name,
            "runtime_instance_id": self.runtime_instance_id,
            "transport": self.config.transport.value,
            "command": self.config.command,
            "args": list(self.config.args),
            "url": self.config.url,
            "enabled": self.config.enabled,
            "state": self.state.value,
            "connected": self.ready,
            "pid": None,
            "tool_count": len(self.registered_names),
            "server_name": info.name if info else "",
            "server_version": info.version if info else "",
            "protocol_version": info.protocol_version if info else "",
            "last_error": self._last_error,
            "last_call_error": self._last_call_error,
            "last_connected_at": self._last_connected_at,
            "last_disconnected_at": self._last_disconnected_at,
            "connection_attempts": self._connection_attempts,
            "reconnect_attempts": self._reconnect_attempts,
            "next_retry_at": self._next_retry_at,
            "tool_refresh_count": self._tool_refresh_count,
            "notification_count": self._notification_count,
            "initial_attempt_done": self._initial_attempt_done.is_set(),
            "initial_attempt_duration_seconds": self._initial_attempt_duration_seconds,
            "last_shutdown_error": self._last_shutdown_error,
            "shutdown_timeout_count": self._shutdown_timeout_count,
            "stderr_tail": stderr_tail,
        }

    async def _run(self) -> None:
        failure_index = 0
        first_attempt = True
        try:
            while not self._stop_event.is_set():
                permanent_failure = False
                self.state = MCPRuntimeState.CONNECTING if first_attempt else MCPRuntimeState.RECONNECTING
                self._connection_attempts += 1
                connection = self._connection_factory(self.config, self._on_notification)
                self._connection = connection
                try:
                    info = await connection.connect()
                    tools = await connection.list_tools()
                    if self._registrar.sync(tools) and self._registrar.global_active:
                        await self._notify_tools_changed()
                    self._server_info = info
                    self._last_error = ""
                    self._stderr_tail = []
                    self._last_connected_at = _now()
                    self._next_retry_at = 0.0
                    self.state = MCPRuntimeState.READY
                    self._registrar.set_available(True)
                    failure_index = 0
                    if first_attempt:
                        self._mark_initial_attempt_done()
                    first_attempt = False
                    reason = await self._ready_loop(connection)
                    if reason == "stop":
                        break
                    raise ConnectionError(self._last_error or "MCP connection requires recovery")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._last_error = _error_text(exc, self.config)
                    permanent_failure = _is_permanent_error(exc)
                    self.state = MCPRuntimeState.FAILED if permanent_failure else MCPRuntimeState.RECONNECTING
                    self._registrar.set_available(False)
                    if first_attempt:
                        self._mark_initial_attempt_done()
                    first_attempt = False
                finally:
                    self._connection = None
                    try:
                        await connection.close()
                    except Exception:
                        logger.debug("MCP server '%s' close failed", self.config.name, exc_info=True)
                    if isinstance(connection, SDKMCPConnection):
                        self._stderr_tail = connection.stderr_tail()
                    self._last_disconnected_at = _now()

                if permanent_failure:
                    break
                if self._stop_event.is_set():
                    break
                delay = self._reconnect_delays[min(failure_index, len(self._reconnect_delays) - 1)] + self._jitter()
                failure_index += 1
                self._reconnect_attempts += 1
                self._next_retry_at = time.time() + delay
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._mark_initial_attempt_done()
            self._connection = None
            self._registrar.set_available(False)

    def _mark_initial_attempt_done(self) -> None:
        if self._initial_attempt_done.is_set():
            return
        if self._initial_attempt_started_at > 0:
            self._initial_attempt_duration_seconds = max(
                time.monotonic() - self._initial_attempt_started_at,
                0.0,
            )
        self._initial_attempt_done.set()

    async def _ready_loop(self, connection: MCPConnection) -> str:
        self._reconnect_event.clear()
        while not self._stop_event.is_set():
            signal = await self._wait_for_signal()
            # stop and reconnect can be set together by shutdown. Stopping
            # wins so a normal close is never recorded as a recovery error.
            if signal == "stop" or self._stop_event.is_set():
                return "stop"
            if signal == "reconnect":
                self._reconnect_event.clear()
                return "reconnect"
            if signal == "refresh":
                self._refresh_event.clear()
                if self._refresh_debounce > 0:
                    await asyncio.sleep(self._refresh_debounce)
                try:
                    if (
                        self._registrar.sync(await connection.list_tools())
                        and self._registrar.global_active
                    ):
                        await self._notify_tools_changed()
                    self._tool_refresh_count += 1
                    self._last_error = ""
                    self.state = MCPRuntimeState.READY
                    self._registrar.set_available(True)
                except Exception as exc:
                    self._last_error = _error_text(exc, self.config)
                    self.state = MCPRuntimeState.DEGRADED
                continue
            try:
                await connection.ping()
            except Exception as exc:
                self._last_error = _error_text(exc, self.config)
                return "reconnect"
        return "stop"

    async def _notify_tools_changed(self) -> None:
        if self._on_tools_changed is None:
            return
        result = self._on_tools_changed(
            self.config.name,
            self.runtime_instance_id,
            self.registered_names,
        )
        if inspect.isawaitable(result):
            await result

    async def _wait_for_signal(self) -> str:
        tasks = {
            asyncio.create_task(self._stop_event.wait()): "stop",
            asyncio.create_task(self._reconnect_event.wait()): "reconnect",
            asyncio.create_task(self._refresh_event.wait()): "refresh",
        }
        done, pending = await asyncio.wait(
            tasks,
            timeout=self._health_interval,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if not done:
            return "health"
        task = next(iter(done))
        return tasks[task]

    def _on_notification(self, name: str) -> None:
        self._notification_count += 1
        if name == "tools/list_changed":
            self._refresh_event.set()

    def _create_connection(self, config: MCPServerConfig, callback) -> MCPConnection:
        return SDKMCPConnection(
            config,
            notification_callback=callback,
            env_values=self._env_values,
            process_backend=self._process_backend,
            sandbox_roots=self._sandbox_roots,
            work_dir=self._work_dir,
        )

    def _reset_events(self) -> None:
        self._stop_event = asyncio.Event()
        self._reconnect_event = asyncio.Event()
        self._refresh_event = asyncio.Event()
        self._initial_attempt_done = asyncio.Event()


def _unavailable_result(server_name: str, detail: str = "") -> MCPCallResult:
    text = f"MCP server temporarily unavailable: {server_name}"
    if detail:
        text = f"{text} ({detail})"
    return MCPCallResult(text=text, is_error=True, metadata={"reason": "temporarily_unavailable"})


def _error_text(exc: BaseException, config: MCPServerConfig) -> str:
    if isinstance(exc, FileNotFoundError):
        return f"command not found: {config.command}"
    return f"{type(exc).__name__}: {exc}"


def _is_permanent_error(exc: BaseException) -> bool:
    return isinstance(exc, ValueError)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _resolve_work_dir(base: Path, configured: str) -> Path:
    root = base.resolve()
    if not configured:
        return root
    candidate = (root / configured).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("MCP work_dir must remain inside the MCP data directory") from exc
    return candidate
