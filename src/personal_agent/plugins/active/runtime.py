"""Runtime control and root-task ownership for one active plugin generation."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from typing import Any

from personal_agent.plugins.active.contracts import ActiveRegistration, ActiveRunnerState


class ActiveRuntimeControl:
    def __init__(self, *, plugin, scope) -> None:
        self.plugin = plugin
        self.scope = scope
        self.state = ActiveRunnerState.DISABLED
        self.ready_at: datetime | None = None
        self.started_at: datetime | None = None
        self.last_heartbeat: datetime | None = None
        self.last_error = ""
        self.restart_count = 0
        self._ready = asyncio.Event()
        self._committed = asyncio.Event()
        self._resume = asyncio.Event()
        self._resume.set()
        self._stop = asyncio.Event()
        self._aborted = False

    @property
    def quiescing(self) -> bool:
        return not self._resume.is_set()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    async def ready(self) -> None:
        if self._stop.is_set():
            raise asyncio.CancelledError
        self.ready_at = datetime.now(UTC)
        self.state = ActiveRunnerState.READY
        self._ready.set()
        await self._committed.wait()
        if self._aborted or self._stop.is_set():
            raise asyncio.CancelledError

    async def wait_ready(self, timeout: float) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    def commit(self) -> None:
        self.state = ActiveRunnerState.ACTIVE
        self._committed.set()

    def abort(self, error: str = "") -> None:
        self._aborted = True
        self.last_error = str(error or self.last_error)
        self.state = ActiveRunnerState.FAILED
        self._stop.set()
        self._committed.set()
        self._resume.set()

    def request_quiesce(self) -> None:
        self.state = ActiveRunnerState.QUIESCING
        self._resume.clear()

    def resume(self) -> None:
        self.state = ActiveRunnerState.ACTIVE
        self._resume.set()

    async def wait_until_resumed(self) -> None:
        await self._resume.wait()

    def request_stop(self) -> None:
        self.state = ActiveRunnerState.STOPPING
        self._stop.set()
        self._resume.set()
        self._committed.set()

    def defer(self, *, name: str, cleanup) -> None:
        self.scope.defer(name, cleanup)

    def heartbeat(self) -> None:
        self.last_heartbeat = datetime.now(UTC)

    def safe_summary(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "ready": self._ready.is_set() and not self._aborted,
            "started_at": self.started_at.isoformat() if self.started_at else "",
            "ready_at": self.ready_at.isoformat() if self.ready_at else "",
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else "",
            "last_error": self.last_error,
            "restart_count": self.restart_count,
            "quiescing": self.quiescing,
            "stop_requested": self.stop_requested,
        }


class ActivePluginRunner:
    def __init__(self, *, plugin, registration: ActiveRegistration, context, scope) -> None:
        self.plugin = plugin
        self.registration = registration
        self.context = context
        self.control = ActiveRuntimeControl(plugin=plugin, scope=scope)
        self.root_task: asyncio.Task[None] | None = None

    def start(self) -> asyncio.Task[None]:
        if self.root_task is not None and not self.root_task.done():
            return self.root_task
        self.control.state = ActiveRunnerState.STARTING
        self.control.started_at = datetime.now(UTC)
        self.root_task = asyncio.create_task(
            self._run(),
            name=f"plugin-active:{self.plugin.key}:{self.plugin.runtime_instance_id}",
        )
        return self.root_task

    async def wait_ready(self) -> None:
        """Wait for readiness, failing immediately if the root runner exits first."""
        task = self.root_task
        if task is None:
            raise RuntimeError(f"active plugin runner has not started: {self.plugin.key}")
        ready_waiter = asyncio.create_task(
            self.control.wait_ready(self.registration.startup_timeout),
            name=f"plugin-active-ready:{self.plugin.key}",
        )
        done, _ = await asyncio.wait(
            {task, ready_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if ready_waiter in done:
            await ready_waiter
            return
        ready_waiter.cancel()
        await asyncio.gather(ready_waiter, return_exceptions=True)
        if task.cancelled():
            raise RuntimeError(f"active plugin stopped before readiness: {self.plugin.key}")
        error = task.exception()
        if error is not None:
            raise RuntimeError(
                f"active plugin failed before readiness: {self.plugin.key}: "
                f"{type(error).__name__}: {error}"
            ) from error
        raise RuntimeError(f"active plugin exited before readiness: {self.plugin.key}")

    async def _run(self) -> None:
        try:
            await self.registration.run(self.context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.control.last_error = f"{type(exc).__name__}: {exc}"
            self.control.state = ActiveRunnerState.FAILED
            raise
        else:
            self.control.state = ActiveRunnerState.STOPPED

    async def quiesce(self) -> None:
        self.control.request_quiesce()
        await _invoke(self.registration.on_quiesce, self.context)

    async def resume(self) -> None:
        await _invoke(self.registration.on_resume, self.context)
        self.control.resume()

    async def stop(self) -> None:
        self.control.request_stop()
        await _invoke(self.registration.on_stop, self.context)
        task = self.root_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), self.registration.shutdown_timeout)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self.control.state is not ActiveRunnerState.FAILED:
            self.control.state = ActiveRunnerState.STOPPED


async def _invoke(callback, context) -> None:
    if callback is None:
        return
    result = callback(context)
    if inspect.isawaitable(result):
        await result
