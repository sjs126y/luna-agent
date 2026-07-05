"""Shared application runtime bootstrap."""

from __future__ import annotations

import pytest

from personal_agent.config import Settings
from personal_agent.runtime import create_app_runtime, start_mcp_manager


def _write_memory_plugin(plugin_dir):
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: user/memory
name: User Memory
version: 1.0.0
entrypoint: memory_plugin:register
enabled_by_default: true
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "memory_plugin.py").write_text(
        """
class Memory:
    async def prefetch(self, user_message):
        return []

    async def save(self, content):
        return None

    async def search(self, query):
        return []

    async def load_all(self):
        return []

    def get_system_prompt_text(self):
        return "memory-ok"

def create_builtin_memory_provider(system_dir=None, **kwargs):
    return Memory()

def register(ctx):
    ctx.register_hook("create_builtin_memory_provider", create_builtin_memory_provider, priority=1)
""".strip(),
        encoding="utf-8",
    )


def _write_mcp_plugin(plugin_dir):
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """
key: user/mcp
name: User MCP
version: 1.0.0
entrypoint: mcp_plugin:register
enabled_by_default: true
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "mcp_plugin.py").write_text(
        """
def register(ctx):
    ctx.register_mcp_server({
        "name": "demo",
        "command": "python",
        "args": ["-m", "demo"],
        "enabled": True,
    })
""".strip(),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_create_app_runtime_initializes_shared_resources(tmp_path):
    plugins_dir = tmp_path / "plugins"
    _write_memory_plugin(plugins_dir / "memory")
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[plugins_dir],
        plugins_enabled=["user/memory"],
        plugins_disabled=["memory/file", "memory/embedding"],
        mcp_enabled=False,
    )

    runtime = await create_app_runtime(settings)
    try:
        assert runtime.settings is settings
        assert runtime.plugin_manager.get_command("missing") is None
        assert runtime.db is not None
        assert runtime.session_store is not None
        assert runtime.compression_chain is not None
        assert runtime.memory_manager.get_system_prompt_text() == "memory-ok"
        assert runtime.conversation_service.session_store is runtime.session_store
        assert runtime.conversation_service.memory_manager is runtime.memory_manager
        assert runtime.memory_review_service is not None
        assert (runtime.system_dir / "AGENT.md").exists()
        assert runtime.mcp_manager is None
        boot = runtime.boot_report.as_dict()
        boot_steps = {step["name"]: step for step in boot["steps"]}
        assert boot["ok"] is True
        assert boot["failed_step"] == ""
        assert boot_steps["settings"]["status"] == "ok"
        assert boot_steps["plugins.discover"]["status"] == "ok"
        assert boot_steps["plugins.load_enabled"]["status"] == "ok"
        assert boot_steps["plugins.configure"]["status"] == "ok"
        assert boot_steps["sandbox"]["status"] == "ok"
        assert boot_steps["mcp"]["status"] == "skipped"
        assert boot_steps["database"]["status"] == "ok"
        assert boot_steps["memory"]["status"] == "ok"
        assert boot_steps["conversation"]["status"] == "ok"
        assert boot_steps["runtime"]["status"] == "ok"
        health = runtime.health_snapshot()
        assert health["db_open"] is True
        assert health["mcp"]["running"] is False
        assert health["mcp"]["total_tools"] == 0
        assert health["boot"]["ok"] is True
        assert health["boot_ok"] is True
        assert health["boot_failed_step"] == ""
        assert health["gateway_created"] is False
        assert health["gateway_running"] is False
        assert health["gateway"] == {}
        assert health["cached_agents"] == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_create_app_runtime_requires_builtin_memory(tmp_path):
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[],
        plugins_disabled=["memory/file", "memory/embedding"],
        mcp_enabled=False,
    )

    with pytest.raises(RuntimeError, match="No built-in memory provider"):
        await create_app_runtime(settings)


@pytest.mark.asyncio
async def test_create_app_runtime_cleans_up_on_start_failure(tmp_path, monkeypatch):
    stopped = []

    class FakeMCPManager:
        def __init__(self, configs):
            self.configs = configs

        async def start(self):
            return None

        async def stop(self):
            stopped.append(True)

    monkeypatch.setattr("personal_agent.mcp.manager.MCPManager", FakeMCPManager)
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[],
        plugins_disabled=["memory/file", "memory/embedding"],
        mcp_enabled=True,
        mcp_servers=[{"name": "config", "command": "python", "args": [], "enabled": True}],
    )

    with pytest.raises(RuntimeError, match="No built-in memory provider"):
        await create_app_runtime(settings)

    assert stopped == [True]


@pytest.mark.asyncio
async def test_create_app_runtime_reports_mcp_boot_step(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    _write_memory_plugin(plugins_dir / "memory")
    _write_mcp_plugin(plugins_dir / "mcp")

    class FakeMCPManager:
        def __init__(self, configs):
            self.configs = configs
            self.stopped = False

        async def start(self):
            return None

        async def stop(self):
            self.stopped = True

        def health_snapshot(self):
            return {
                "running": not self.stopped,
                "configured_count": len(self.configs),
                "connected_count": len(self.configs),
                "total_tools": 0,
                "registered_tools": [],
                "servers": [],
            }

    monkeypatch.setattr("personal_agent.mcp.manager.MCPManager", FakeMCPManager)
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[plugins_dir],
        plugins_enabled=["user/memory", "user/mcp"],
        plugins_disabled=["memory/file", "memory/embedding"],
        mcp_enabled=True,
    )

    runtime = await create_app_runtime(settings)
    try:
        boot_steps = {step["name"]: step for step in runtime.boot_report.as_dict()["steps"]}
        assert boot_steps["mcp"]["status"] == "ok"
        assert boot_steps["mcp"]["detail"].startswith("servers=")
        assert runtime.health_snapshot()["mcp_running"] is True
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_app_runtime_gateway_lifecycle(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    _write_memory_plugin(plugins_dir / "memory")
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[plugins_dir],
        plugins_enabled=["user/memory"],
        plugins_disabled=["memory/file", "memory/embedding"],
        mcp_enabled=False,
    )
    started = []
    stopped = []

    async def start(self):
        started.append(self)

    async def stop(self):
        stopped.append(self)

    monkeypatch.setattr("personal_agent.gateway.gateway.Gateway.start", start)
    monkeypatch.setattr("personal_agent.gateway.gateway.Gateway.stop", stop)

    runtime = await create_app_runtime(settings)
    try:
        gateway = runtime.create_gateway(system_prompt_template="system")
        assert runtime.gateway is gateway
        assert gateway._conversation_service is runtime.conversation_service
        assert gateway._memory_review_service is runtime.memory_review_service

        started_gateway = await runtime.start_gateway(system_prompt_template="system")
        assert started_gateway is gateway
        assert started == [gateway]
        assert runtime.health_snapshot()["gateway_running"] is True
        assert "gateway" in runtime.health_snapshot()

        await runtime.stop_gateway()
        assert stopped == [gateway]
        assert runtime.gateway is None
        assert runtime.gateway_started is False

        await runtime.close()
        await runtime.close()
        assert runtime.closed is True
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_start_mcp_manager_merges_plugin_servers(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    _write_mcp_plugin(plugins_dir / "mcp")
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[plugins_dir],
        plugins_enabled=["user/mcp"],
        mcp_enabled=True,
        mcp_servers=[{"name": "config", "command": "python", "args": [], "enabled": True}],
    )

    from personal_agent.plugins.core.manager import PluginManager

    plugin_manager = PluginManager(settings)
    plugin_manager.discover()
    plugin_manager.load_enabled()

    created = {}

    class FakeMCPManager:
        def __init__(self, configs):
            created["configs"] = configs
            self.started = False

        async def start(self):
            self.started = True

    monkeypatch.setattr("personal_agent.mcp.manager.MCPManager", FakeMCPManager)

    manager = await start_mcp_manager(settings, plugin_manager)

    assert manager.started
    assert [item["name"] for item in created["configs"]] == ["config", "demo"]
