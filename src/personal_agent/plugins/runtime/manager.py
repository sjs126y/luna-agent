"""Narrow control facade for live plugin generation operations."""

from __future__ import annotations

from pathlib import Path


class PluginRuntimeManager:
    def __init__(self, plugin_manager) -> None:
        self._plugins = plugin_manager

    @property
    def capability_store(self):
        return self._plugins.capability_store

    async def install(self, source: Path | str, *, enable: bool = True):
        return await self._plugins.install_plugin_runtime(source, enable=enable)

    async def reload(self, key: str):
        return await self._plugins.reload_plugin_runtime(key)

    async def enable(self, key: str):
        return await self._plugins.enable_plugin_runtime(key)

    async def disable(self, key: str):
        return await self._plugins.disable_plugin_runtime(key)

    async def rollback(self, key: str, digest: str):
        return await self._plugins.rollback_plugin_runtime(key, digest)

    async def uninstall(self, key: str, *, purge_data: bool = False):
        return await self._plugins.uninstall_plugin_runtime(key, purge_data=purge_data)

    async def start_active(self):
        await self._plugins.start_active_plugins()

    async def stop_active(self):
        await self._plugins.stop_active_plugins()

    async def set_active(self, key: str, enabled: bool):
        return await self._plugins.set_active_enabled(key, enabled)

    async def restart_active(self, key: str):
        return await self._plugins.restart_active_plugin(key)

    def health_snapshot(self):
        return self._plugins.capability_health()
