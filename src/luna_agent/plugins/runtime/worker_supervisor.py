"""Worker process ownership and recovery for external plugin generations."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from luna_agent.plugins.runtime.models import PluginRuntimeState
from luna_agent.plugins.runtime.worker_client import PluginWorkerClient

logger = logging.getLogger(__name__)


class WorkerSupervisor:
    """Own Worker instances, leases, recovery tasks, backoff, and circuit state."""

    def __init__(self, service) -> None:
        self.service = service
        self.manager = service.manager
        self.workers: dict[str, PluginWorkerClient] = {}
        self._specs: dict[str, dict[str, Any]] = {}
        self._recovery_tasks: dict[str, asyncio.Task[Any]] = {}
        self._stopping: set[str] = set()
        self._environment_leases: dict[str, Any] = {}

    @property
    def recovery_tasks(self) -> tuple[asyncio.Task[Any], ...]:
        return tuple(self._recovery_tasks.values())

    def start(self, plugin, *, environment, config: dict[str, Any]) -> None:
        if plugin.manifest.path is None:
            raise ValueError(f"Plugin package root is unavailable: {plugin.key}")
        if plugin.data_path is None:
            raise RuntimeError(f"Plugin data revision is unavailable: {plugin.key}")
        runtime_id = plugin.runtime_instance_id
        self._stopping.discard(runtime_id)
        lease = self.service.environments.acquire_lease(
            plugin.key,
            environment.environment_id,
            runtime_id,
        )
        try:
            worker, result, launch = self.service._spawn_worker(
                plugin, environment=environment, config=config
            )
        except Exception:
            lease.close()
            raise
        self.workers[runtime_id] = worker
        self._environment_leases[runtime_id] = lease
        self._specs[runtime_id] = {
            "environment": environment,
            "config": dict(config),
            "capabilities": dict(result.get("capabilities") or {}),
            "launch_backend": launch.backend,
            "sandbox_cleanup": launch.cleanup,
        }
        plugin.worker = worker
        plugin.environment_id = environment.environment_id
        plugin.environment_path = environment.root
        plugin.sandbox_backend = launch.backend
        plugin.worker_capabilities = dict(result.get("capabilities") or {})
        plugin.worker_state = "running"
        plugin.worker_last_error = ""
        plugin.worker_next_retry_at = ""
        self.service._register_capabilities(plugin, plugin.worker_capabilities)

    def worker_exited(
        self,
        plugin,
        worker: PluginWorkerClient,
        summary: dict[str, Any],
    ) -> None:
        runtime_id = str(getattr(plugin, "runtime_instance_id", "") or "")
        if runtime_id in self._stopping or self.workers.get(runtime_id) is not worker:
            return
        task = self._recovery_tasks.get(runtime_id)
        if task is not None and not task.done():
            return
        self._recovery_tasks[runtime_id] = asyncio.create_task(
            self._recover(plugin, worker, summary),
            name=f"plugin-worker-recovery:{plugin.key}:{runtime_id}",
        )
        self._recovery_tasks[runtime_id].add_done_callback(
            lambda done, _runtime_id=runtime_id: self._discard_recovery(
                _runtime_id, done
            )
        )

    def _discard_recovery(self, runtime_id: str, task: asyncio.Task[Any]) -> None:
        if self._recovery_tasks.get(runtime_id) is task:
            self._recovery_tasks.pop(runtime_id, None)
        error = None if task.cancelled() else task.exception()
        if error is not None:
            logger.error(
                "Plugin Worker recovery task failed: runtime=%s error=%s",
                runtime_id,
                error,
            )

    async def _recover(
        self,
        plugin,
        exited_worker: PluginWorkerClient,
        summary: dict[str, Any],
    ) -> None:
        runtime_id = str(getattr(plugin, "runtime_instance_id", "") or "")
        spec = self._specs.get(runtime_id)
        if spec is None or self.workers.get(runtime_id) is not exited_worker:
            return
        recovery_state = plugin.runtime_state
        plugin.worker_state = "recovering"
        self.manager.generation_coordinator.transition(
            plugin,
            PluginRuntimeState.FAILED,
            reason="worker_exited",
        )
        plugin.worker_last_error = str(
            summary.get("last_error") or summary.get("stderr_tail") or "worker exited"
        )[-8000:]
        plugin.worker_last_exit_at = datetime.now(timezone.utc).isoformat()
        self.manager.events.record(
            plugin.key,
            "worker_crashed",
            level="error",
            details={"runtime_instance_id": runtime_id, **summary},
        )
        try:
            await asyncio.to_thread(exited_worker.stop)
        except Exception:
            logger.exception("Failed to close exited plugin Worker: %s", plugin.key)
        old_cleanup = spec.get("sandbox_cleanup")
        if callable(old_cleanup):
            try:
                await asyncio.to_thread(old_cleanup)
            except Exception:
                logger.exception("Failed to clean exited Worker sandbox: %s", plugin.key)
            else:
                spec["sandbox_cleanup"] = None
        active_supervisor = self.manager.active_supervisor
        should_restart_active = bool(
            plugin.active_registration is not None
            and plugin.active_enabled
            and active_supervisor.owner_running
            and self.manager._plugins.get(plugin.key) is plugin
        )
        active_was_running = bool(
            plugin.active_runner is not None
            and plugin.active_runner.root_task is not None
            and not plugin.active_runner.root_task.done()
        )
        if active_was_running:
            await active_supervisor.stop(plugin)

        loop = asyncio.get_running_loop()
        now = loop.time()
        window = float(
            getattr(self.manager.settings, "plugin_worker_restart_failure_window", 300.0)
        )
        limit = int(
            getattr(self.manager.settings, "plugin_worker_restart_failure_limit", 5)
        )
        plugin.worker_failure_times = [
            value for value in plugin.worker_failure_times if now - value <= window
        ]
        plugin.worker_failure_times.append(now)
        if len(plugin.worker_failure_times) >= max(1, limit):
            plugin.worker_state = "circuit_open"
            plugin.worker_circuit_open = True
            plugin.worker_next_retry_at = ""
            self.manager.events.record(
                plugin.key,
                "worker_circuit_opened",
                level="error",
                details={"restart_count": plugin.worker_restart_count},
            )
            return

        delays = self._restart_delays()
        delay = delays[min(plugin.worker_restart_count, len(delays) - 1)]
        plugin.worker_restart_count += 1
        plugin.worker_next_retry_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay)
        ).isoformat()
        self.manager.events.record(
            plugin.key,
            "worker_restart_scheduled",
            details={
                "delay_seconds": delay,
                "restart_count": plugin.worker_restart_count,
            },
        )
        await asyncio.sleep(delay)
        if runtime_id in self._stopping or self.workers.get(runtime_id) is not exited_worker:
            return
        spawn_task = asyncio.create_task(
            asyncio.to_thread(
                self.service._spawn_worker,
                plugin,
                environment=spec["environment"],
                config=spec["config"],
                host_loop=loop,
            ),
            name=f"plugin-worker-spawn:{plugin.key}:{runtime_id}",
        )
        try:
            worker, result, launch = await asyncio.shield(spawn_task)
            capabilities = dict(result.get("capabilities") or {})
            if _capability_fingerprint(capabilities) != _capability_fingerprint(
                spec["capabilities"]
            ):
                worker.stop()
                if launch.cleanup is not None:
                    launch.cleanup()
                raise RuntimeError("Worker capability contract changed during recovery")
            self.workers[runtime_id] = worker
            spec["sandbox_cleanup"] = launch.cleanup
            plugin.worker = worker
            plugin.sandbox_backend = launch.backend
            plugin.worker_state = "running"
            self.manager.generation_coordinator.transition(
                plugin,
                recovery_state,
                reason="worker_recovered",
            )
            plugin.worker_circuit_open = False
            plugin.worker_last_error = ""
            plugin.worker_next_retry_at = ""
            self.manager._publish_current_bindings()
            self.manager.events.record(
                plugin.key,
                "worker_restarted",
                details={
                    "runtime_instance_id": runtime_id,
                    "restart_count": plugin.worker_restart_count,
                },
            )
            if should_restart_active:
                await active_supervisor.start(plugin)
        except asyncio.CancelledError:
            try:
                late_worker, _result, late_launch = await spawn_task
            except Exception:
                pass
            else:
                await asyncio.to_thread(late_worker.stop)
                if late_launch.cleanup is not None:
                    await asyncio.to_thread(late_launch.cleanup)
            raise
        except Exception as exc:
            plugin.worker_last_error = f"{type(exc).__name__}: {exc}"
            plugin.worker_state = "failed"
            self.manager.events.record(
                plugin.key,
                "worker_restart_failed",
                level="error",
                details={"error": plugin.worker_last_error},
            )
            self.workers[runtime_id] = exited_worker
            self._recovery_tasks.pop(runtime_id, None)
            self.worker_exited(
                plugin,
                exited_worker,
                {"last_error": plugin.worker_last_error},
            )

    def current_worker(self, plugin) -> PluginWorkerClient:
        runtime_id = str(getattr(plugin, "runtime_instance_id", "") or "")
        worker = self.workers.get(runtime_id)
        if worker is None or not worker.running or plugin.worker_state != "running":
            raise RuntimeError(f"Plugin worker is unavailable: {plugin.key}")
        return worker

    def stop(self, plugin) -> None:
        runtime_id = str(getattr(plugin, "runtime_instance_id", "") or "")
        self._stopping.add(runtime_id)
        recovery = self._recovery_tasks.pop(runtime_id, None)
        if recovery is not None and not recovery.done():
            recovery.cancel()
        worker = self.workers.pop(runtime_id, None) or getattr(plugin, "worker", None)
        if worker is not None:
            try:
                worker.stop()
            except Exception:
                logger.exception("Failed to stop plugin Worker: %s", plugin.key)
        try:
            self.service.processes.stop_generation(runtime_id)
        except Exception:
            logger.exception("Failed to stop plugin host processes: %s", plugin.key)
        spec = self._specs.pop(runtime_id, None)
        cleanup = spec.get("sandbox_cleanup") if isinstance(spec, dict) else None
        if callable(cleanup):
            try:
                cleanup()
            except Exception:
                logger.exception("Failed to clean plugin Worker sandbox: %s", plugin.key)
        lease = self._environment_leases.pop(runtime_id, None)
        if lease is not None:
            try:
                lease.close()
            except Exception:
                logger.exception("Failed to close plugin environment lease: %s", plugin.key)
        plugin.worker = None
        plugin.worker_state = "stopped"
        plugin.worker_next_retry_at = ""
        self._stopping.discard(runtime_id)

    def summary(self, plugin) -> dict[str, Any]:
        worker = self.workers.get(plugin.runtime_instance_id)
        return {
            "isolated": worker is not None,
            "environment_id": str(getattr(plugin, "environment_id", "") or ""),
            "environment_path": str(getattr(plugin, "environment_path", "") or ""),
            "sandbox_backend": str(getattr(plugin, "sandbox_backend", "") or ""),
            "worker": worker.safe_summary() if worker is not None else {},
            **plugin.worker_status.safe_summary(),
        }

    def health_snapshot(self) -> dict[str, int]:
        return {
            "worker_count": len(self.workers),
            "running_count": sum(1 for worker in self.workers.values() if worker.running),
            "recovery_task_count": sum(
                1 for task in self._recovery_tasks.values() if not task.done()
            ),
            "environment_lease_count": len(self._environment_leases),
            "stopping_count": len(self._stopping),
        }

    def close(self, plugins) -> None:
        for task in self.recovery_tasks:
            if not task.done():
                task.cancel()
        for plugin in tuple(plugins):
            self.stop(plugin)

    async def aclose(self, plugins) -> None:
        recovery_tasks = self.recovery_tasks
        for task in recovery_tasks:
            if not task.done():
                task.cancel()
        if recovery_tasks:
            await asyncio.gather(*recovery_tasks, return_exceptions=True)
        await asyncio.gather(*(
            asyncio.to_thread(self.stop, plugin) for plugin in tuple(plugins)
        ))

    def _restart_delays(self) -> tuple[float, ...]:
        try:
            configured = getattr(
                self.manager.settings,
                "plugin_worker_restart_backoff",
                (1.0, 2.0, 5.0, 10.0, 30.0),
            )
            return tuple(
                float(value) for value in configured if float(value) >= 0
            ) or (1.0,)
        except (TypeError, ValueError):
            return (1.0, 2.0, 5.0, 10.0, 30.0)


def _capability_fingerprint(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
