"""Lifecycle supervision for active plugin executions."""

from __future__ import annotations

import asyncio
import logging

from luna_agent.plugins.active.execution import create_active_execution
from luna_agent.plugins.core.models import LoadedPlugin, PluginStatus

logger = logging.getLogger(__name__)


class ActiveSupervisor:
    """Own active root tasks, readiness, restart policy, and circuit state."""

    def __init__(self, manager) -> None:
        self.manager = manager
        self.owner_running = False
        self._lifecycle_lock = asyncio.Lock()
        self._watch_tasks: dict[str, asyncio.Task] = {}

    async def start_all(self) -> None:
        async with self._lifecycle_lock:
            if self.owner_running:
                return
            self.owner_running = True
            for plugin in list(self.manager._plugins.values()):
                if plugin.status is not PluginStatus.LOADED:
                    continue
                if plugin.active_enabled and plugin.active_runner is not None:
                    await self.start(plugin)

    async def stop_all(self) -> None:
        async with self._lifecycle_lock:
            self.owner_running = False
            runners = [
                plugin
                for plugin in self.manager._runtime_records.values()
                if plugin.active_runner is not None
            ]
            await asyncio.gather(
                *(self.stop(plugin) for plugin in runners),
                return_exceptions=True,
            )

    async def restart(self, plugin: LoadedPlugin) -> None:
        await self.stop(plugin)
        plugin.active_circuit_open = False
        plugin.active_failure_times.clear()
        plugin.active_error = ""
        plugin.active_runner = self.create_execution(plugin)
        if self.owner_running:
            await self.start(plugin)

    def trigger(self, plugin: LoadedPlugin, reason: str = "manual") -> None:
        runner = plugin.active_runner
        if plugin.active_registration is None or runner is None:
            raise ValueError(f"plugin does not register an active runner: {plugin.key}")
        if not plugin.active_enabled:
            raise ValueError(f"active plugin is disabled: {plugin.key}")
        if not self.owner_running:
            raise RuntimeError("active plugin owner is not running")
        runner.control.wake(reason)

    def create_execution(self, plugin: LoadedPlugin):
        return create_active_execution(
            plugin=plugin,
            registration=plugin.active_registration,
            context=plugin.ctx,
            scope=plugin.generation_scope,
        )

    async def start(self, plugin: LoadedPlugin) -> None:
        runner = plugin.active_runner
        if runner is None or not plugin.active_enabled:
            return
        if runner.root_task is not None and not runner.root_task.done():
            return
        if runner.root_task is not None:
            runner = self.create_execution(plugin)
            plugin.active_runner = runner
        prepared_data = plugin.data_path is None
        data_commit = None
        if prepared_data:
            self.manager.data_revisions.prepare(plugin, candidate=True)
        plugin.active_error = ""
        try:
            await self.wait_required_mcp(plugin)
            runner.start()
            await runner.wait_ready()
            if prepared_data:
                data_commit = self.manager.data_revisions.commit(plugin)
            runner.control.commit()
            self.watch(plugin, runner)
            if data_commit is not None:
                data_commit.finalize()
            self.manager.events.record(
                plugin.key,
                "active_started",
                operation_id=self.manager.operations.current_operation_id(),
                details={"runtime_instance_id": plugin.runtime_instance_id},
            )
            logger.info("Active plugin started: %s", plugin.key)
        except Exception as exc:
            plugin.active_error = f"{type(exc).__name__}: {exc}"
            runner.control.abort(plugin.active_error)
            await runner.stop()
            if prepared_data:
                if data_commit is not None and not data_commit.finalized:
                    data_commit.rollback()
                else:
                    self.manager.data_revisions.discard(plugin)
                plugin.data_revision_id = ""
                plugin.data_path = None
            self.manager.events.record(
                plugin.key,
                "active_start_failed",
                operation_id=self.manager.operations.current_operation_id(),
                level="error",
                details={"error": plugin.active_error},
            )
            logger.exception("Active plugin failed to start: %s", plugin.key)

    async def stop(self, plugin: LoadedPlugin) -> None:
        runner = plugin.active_runner
        if runner is None or runner.root_task is None:
            return
        try:
            await runner.stop()
            self.manager.events.record(
                plugin.key,
                "active_stopped",
                operation_id=self.manager.operations.current_operation_id(),
                details={"runtime_instance_id": plugin.runtime_instance_id},
            )
        except Exception as exc:
            plugin.active_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Active plugin failed to stop: %s", plugin.key)

    def watch(self, plugin: LoadedPlugin, runner) -> None:
        existing = self._watch_tasks.get(plugin.runtime_instance_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._supervise(plugin, runner),
            name=f"plugin-active-watch:{plugin.key}:{plugin.runtime_instance_id}",
        )
        self._watch_tasks[plugin.runtime_instance_id] = task
        task.add_done_callback(
            lambda done, runtime_id=plugin.runtime_instance_id: self._discard_watch(
                runtime_id, done
            )
        )

    def _discard_watch(self, runtime_id: str, task: asyncio.Task) -> None:
        if self._watch_tasks.get(runtime_id) is task:
            self._watch_tasks.pop(runtime_id, None)

    async def _supervise(self, plugin: LoadedPlugin, runner) -> None:
        task = runner.root_task
        if task is None:
            return
        results = await asyncio.gather(task, return_exceptions=True)
        if runner.control.stop_requested or not self.owner_running:
            return
        if self.manager._plugins.get(plugin.key) is not plugin or not plugin.active_enabled:
            return
        failed = bool(results and isinstance(results[0], BaseException))
        policy = runner.registration.restart_policy.value
        if policy == "never" or (policy == "on_failure" and not failed):
            return

        loop = asyncio.get_running_loop()
        now = loop.time()
        plugin.active_failure_times = [
            value for value in plugin.active_failure_times if now - value <= 300.0
        ]
        plugin.active_failure_times.append(now)
        if len(plugin.active_failure_times) >= 5:
            plugin.active_circuit_open = True
            plugin.active_error = "active runner circuit opened after repeated failures"
            self.manager.events.record(
                plugin.key,
                "active_circuit_opened",
                level="error",
                details={"restart_count": plugin.active_restart_count},
            )
            logger.error("Active plugin circuit opened: %s", plugin.key)
            return
        delays = self.restart_delays(plugin.key)
        delay = delays[min(plugin.active_restart_count, len(delays) - 1)]
        plugin.active_restart_count += 1
        await asyncio.sleep(delay)
        if not self.owner_running or self.manager._plugins.get(plugin.key) is not plugin:
            return
        plugin.active_runner = self.create_execution(plugin)
        plugin.active_runner.control.restart_count = plugin.active_restart_count
        self._watch_tasks.pop(plugin.runtime_instance_id, None)
        await self.start(plugin)

    def restart_delays(self, key: str) -> tuple[float, ...]:
        all_config = getattr(self.manager.settings, "plugins_config", {}) or {}
        plugin_config = all_config.get(key, {}) if isinstance(all_config, dict) else {}
        active = plugin_config.get("active", {}) if isinstance(plugin_config, dict) else {}
        configured = active.get("restart_backoff_seconds", ()) if isinstance(active, dict) else ()
        if isinstance(configured, (list, tuple)):
            values = tuple(float(value) for value in configured if float(value) >= 0)
            if values:
                return values
        return (1.0, 2.0, 5.0, 10.0, 30.0)

    async def wait_required_mcp(self, plugin: LoadedPlugin) -> None:
        registration = plugin.active_registration
        required = tuple(registration.resources.required_mcp_servers)
        if not required:
            return
        manager = self.manager._mcp_manager
        if manager is None:
            raise RuntimeError(
                "required MCP runtime is unavailable: " + ", ".join(required)
            )
        deadline = asyncio.get_running_loop().time() + registration.startup_timeout
        while True:
            health = manager.health_snapshot()
            servers = {item["name"]: item for item in health.get("servers", [])}
            missing = [name for name in required if name not in servers]
            if missing:
                raise RuntimeError(
                    "required MCP server is not configured: " + ", ".join(missing)
                )
            pending = [name for name in required if not servers[name].get("connected")]
            if not pending:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    "required MCP server did not become ready: " + ", ".join(pending)
                )
            await asyncio.sleep(0.05)

    def health_snapshot(self) -> dict[str, object]:
        return {
            "owner_running": self.owner_running,
            "watch_task_count": sum(
                1 for task in self._watch_tasks.values() if not task.done()
            ),
        }
