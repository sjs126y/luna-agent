"""Host-side process and RPC client for one external plugin generation."""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from luna_agent_plugin_sdk.worker_protocol import FramedRPCPeer, WorkerProtocolError


ResourceHandler = Callable[[dict[str, Any]], Awaitable[Any] | Any]
ProcessFactory = Callable[[Sequence[str], Path, dict[str, str]], Any]
WorkerExitCallback = Callable[["PluginWorkerClient", dict[str, Any]], Any]


class PluginWorkerClient:
    def __init__(
        self,
        *,
        python: Path | None = None,
        cwd: Path,
        argv: tuple[str, ...] | list[str] | None = None,
        env: dict[str, str] | None = None,
        startup_timeout: float = 30.0,
        shutdown_timeout: float = 5.0,
        max_stderr_chars: int = 64 * 1024,
        process_factory: ProcessFactory | None = None,
        on_exit: WorkerExitCallback | None = None,
        host_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.python = Path(python) if python is not None else None
        self.cwd = Path(cwd)
        self.argv = tuple(str(item) for item in (argv or ()))
        if not self.argv and self.python is None:
            raise ValueError("Plugin worker requires python or argv")
        self.env = dict(env or {})
        self.startup_timeout = max(1.0, float(startup_timeout))
        self.shutdown_timeout = max(0.5, float(shutdown_timeout))
        self.max_stderr_chars = max(1024, int(max_stderr_chars))
        self.process_factory = process_factory
        self.on_exit = on_exit
        self.process: subprocess.Popen[bytes] | None = None
        self.peer: FramedRPCPeer | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.host_loop = host_loop
        self.thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.start_error = ""
        self.last_stderr = ""
        self.resource_handler: ResourceHandler | None = None
        self._stop_requested = False
        self._initialized = False
        self._exit_notified = False

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None and not self.stopped.is_set()

    def set_resource_handler(self, handler: ResourceHandler | None) -> None:
        self.resource_handler = handler

    def start(self, initialize: dict[str, Any]) -> dict[str, Any]:
        if self.running:
            raise RuntimeError("Plugin worker is already running")
        self._stop_requested = False
        self._initialized = False
        self._exit_notified = False
        if self.host_loop is None:
            try:
                self.host_loop = asyncio.get_running_loop()
            except RuntimeError:
                self.host_loop = None
        command = list(self.argv) or [
            str(self.python), "-m", "luna_agent_plugin_sdk.worker"
        ]
        if self.process_factory is not None:
            self.process = self.process_factory(command, self.cwd, dict(self.env))
        else:
            self.process = subprocess.Popen(
                command,
                cwd=str(self.cwd),
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=os.name != "nt",
            )
        self.thread = threading.Thread(
            target=self._thread_main,
            name=f"plugin-worker-rpc:{self.process.pid}",
            daemon=True,
        )
        self.thread.start()
        self.stderr_thread = threading.Thread(
            target=self._stderr_main,
            name=f"plugin-worker-stderr:{self.process.pid}",
            daemon=True,
        )
        self.stderr_thread.start()
        if not self.ready.wait(self.startup_timeout):
            self._terminate()
            raise TimeoutError("Plugin worker RPC did not start")
        if self.start_error:
            self._terminate()
            raise RuntimeError(self.start_error)
        try:
            result = self.call_sync("initialize", initialize, timeout=self.startup_timeout)
        except Exception:
            self._terminate()
            raise
        if not isinstance(result, dict):
            self._terminate()
            raise RuntimeError("Plugin worker returned an invalid initialization result")
        self._initialized = True
        return result

    def call_sync(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> Any:
        if self.loop is None or self.peer is None or not self.running:
            raise RuntimeError("Plugin worker is not running")
        future = asyncio.run_coroutine_threadsafe(
            self.peer.call(method, payload or {}, timeout=timeout),
            self.loop,
        )
        return future.result(timeout=max(0.1, timeout) + 1.0)

    async def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> Any:
        if self.loop is None or self.peer is None or not self.running:
            raise RuntimeError("Plugin worker is not running")
        future = asyncio.run_coroutine_threadsafe(
            self.peer.call(method, payload or {}, timeout=timeout),
            self.loop,
        )
        return await asyncio.wrap_future(future)

    def stop(self) -> None:
        process = self.process
        if process is None:
            return
        self._stop_requested = True
        if process.poll() is None and self.peer is not None and self.loop is not None:
            try:
                self.call_sync("shutdown", {}, timeout=self.shutdown_timeout)
            except Exception:
                pass
        try:
            process.wait(timeout=self.shutdown_timeout)
        except subprocess.TimeoutExpired:
            self._terminate()
        finally:
            self._close_process_handles(process)
        self.stopped.set()

    def safe_summary(self) -> dict[str, Any]:
        process = self.process
        return {
            "pid": self.pid,
            "running": self.running,
            "returncode": process.poll() if process is not None else None,
            "last_error": self.start_error or (self.peer.last_error if self.peer else ""),
            "stderr_tail": self.last_stderr,
        }

    def _notify_unexpected_exit(self) -> None:
        callback = self.on_exit
        if callback is None or self._exit_notified or self._stop_requested or not self._initialized:
            return
        self._exit_notified = True
        summary = self.safe_summary()
        try:
            if self.host_loop is not None and self.host_loop.is_running():
                self.host_loop.call_soon_threadsafe(callback, self, summary)
            else:
                callback(self, summary)
        except Exception:
            # Diagnostics callbacks must never keep the worker monitor thread alive.
            pass

    def _thread_main(self) -> None:
        process = self.process
        if process is None or process.stdout is None or process.stdin is None:
            self.start_error = "Plugin worker pipes are unavailable"
            self.ready.set()
            return
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        peer = FramedRPCPeer(process.stdout, process.stdin)
        self.peer = peer
        peer.register("resource.call", self._resource_call)

        async def run() -> None:
            await peer.start()
            self.ready.set()
            while not peer.closed and process.poll() is None:
                await asyncio.sleep(0.05)
            await peer.close()

        try:
            loop.run_until_complete(run())
        except Exception as exc:
            self.start_error = f"{type(exc).__name__}: {exc}"
            self.ready.set()
        finally:
            self.stopped.set()
            self._notify_unexpected_exit()
            loop.close()

    async def _resource_call(self, payload: dict[str, Any]) -> Any:
        handler = self.resource_handler
        if handler is None:
            raise PermissionError("Plugin host resources are unavailable")
        if self.host_loop is None:
            result = handler(payload)
            if asyncio.iscoroutine(result):
                return await result
            return result
        async def invoke() -> Any:
            result = handler(payload)
            if asyncio.iscoroutine(result):
                return await result
            return result

        future = asyncio.run_coroutine_threadsafe(invoke(), self.host_loop)
        return await asyncio.wrap_future(future)

    def _stderr_main(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        while True:
            chunk = process.stderr.read(4096)
            if not chunk:
                return
            text = chunk.decode("utf-8", errors="replace")
            self.last_stderr = (self.last_stderr + text)[-self.max_stderr_chars:]

    def _terminate(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)

    @staticmethod
    def _close_process_handles(process: Any) -> None:
        """Close custom launcher handles without assuming ``Popen`` semantics."""
        close = getattr(process, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
