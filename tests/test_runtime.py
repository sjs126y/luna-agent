"""Shared application runtime bootstrap."""

from __future__ import annotations

import pytest

from personal_agent.config import Settings
from personal_agent.runtime import boot_report_from_exception, create_app_runtime, start_mcp_manager


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
    ctx.register_mcp("mcp.yaml")
""".strip(),
        encoding="utf-8",
    )
    (plugin_dir / "mcp.yaml").write_text(
        """
servers:
  - name: demo
    transport: stdio
    command: python
    args: [-m, demo]
    enabled: true
""".strip(),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_create_app_runtime_initializes_shared_resources(tmp_path):
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[],
        plugins_disabled=[],
        mcp_enabled=False,
        memory_external_provider="none",
    )

    runtime = await create_app_runtime(settings)
    try:
        assert runtime.settings is settings
        assert runtime.plugin_manager.get_command("missing") is None
        assert runtime.db is not None
        assert runtime.session_store is not None
        assert runtime.compression_chain is not None
        assert "行为规则" in runtime.memory_manager.get_system_prompt_text()
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
        assert health["turns"]["stored"] == 0
        assert health["tool_truth"]["inspected"] == 0
        assert health["tool_runs"]["inspected"] == 0
        assert health["commands"]["registry_version"] == 1
        assert health["commands"]["has_tool_runs"] is True
        assert health["commands"]["has_mode_arguments"] is True
        assert "has_allow_arguments" not in health["commands"]
        assert set(health["commands"]["dynamic_providers"]) >= {"tools", "sessions"}
        assert health["query"]["conversation_query_service"] is True
        assert health["query"]["tool_runs_query"] is True
        assert health["execution"]["mode"] == "ask-first"
        assert health["execution"]["label"] == "Ask First"
        assert health["execution"]["profile"] == "read-only"
        assert health["execution"]["approval_policy"] == "on-request"
        runtime.conversation_service.record_turn_report(
            "cli:default:local",
            type("Source", (), {"platform": "cli", "user_id": "local", "chat_id": "default", "chat_type": "dm"})(),
            {
                "status": "completed",
                "duration": 1.5,
                "error": "",
                "llm": {"calls": 1, "input_tokens": 10, "output_tokens": 5},
                "tools": {"total": 2, "items": []},
                "tool_truth": {
                    "calls_total": 2,
                    "results_total": 2,
                    "llm_tool_call_count": 2,
                    "tool_names": ["bash", "search"],
                    "status_counts": {"success": 1, "denied": 1},
                    "warnings": [],
                    "assistant_claim": {
                        "claimed_tool_use": False,
                        "claim_phrases": [],
                        "claimed_but_no_tool_call": False,
                    },
                },
                "retries": [{}],
            },
        )
        updated_health = runtime.health_snapshot()
        turn_health = updated_health["turns"]
        assert turn_health["stored"] == 1
        assert turn_health["last_status"] == "completed"
        assert turn_health["last_duration"] == 1.5
        assert turn_health["last_llm_calls"] == 1
        assert turn_health["last_tool_calls"] == 2
        assert turn_health["last_retries"] == 1
        truth_health = updated_health["tool_truth"]
        assert truth_health["inspected"] == 1
        assert truth_health["turns_with_tools"] == 1
        assert truth_health["tool_counts"] == {"bash": 1, "search": 1}
        assert truth_health["denied_tool_calls"] == 1
        runtime.conversation_service._recent_tool_runs.append({
            "tool_name": "bash",
            "status": "success",
            "category": "",
            "output_truncated": False,
        })
        runtime.conversation_service._recent_tool_runs.append({
            "tool_name": "write",
            "status": "denied",
            "category": "permission",
            "output_truncated": True,
        })
        run_health = runtime.health_snapshot()["tool_runs"]
        assert run_health["inspected"] == 2
        assert run_health["tool_counts"] == {"bash": 1, "write": 1}
        assert run_health["denied"] == 1
        assert run_health["truncated"] == 1
        assert health["gateway_created"] is False
        assert health["gateway_running"] is False
        assert health["gateway"] == {}
        assert health["cached_agents"] == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_create_app_runtime_internal_memory_is_core(tmp_path):
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[],
        plugins_disabled=[],
        mcp_enabled=False,
        memory_external_provider="none",
    )

    runtime = await create_app_runtime(settings)
    try:
        assert runtime.memory_manager.internal is not None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_create_app_runtime_attaches_boot_report_on_failure(tmp_path, monkeypatch):
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[],
        plugins_disabled=[],
        mcp_enabled=False,
        memory_external_provider="none",
    )

    async def fail_memory(*args, **kwargs):
        raise RuntimeError("memory bootstrap failed")

    monkeypatch.setattr("personal_agent.runtime.create_memory_manager", fail_memory)
    with pytest.raises(RuntimeError, match="memory bootstrap failed") as exc_info:
        await create_app_runtime(settings)

    boot_report = boot_report_from_exception(exc_info.value)
    assert boot_report is not None
    boot = boot_report.as_dict()
    boot_steps = {step["name"]: step for step in boot["steps"]}
    assert boot["ok"] is False
    assert boot["failed_step"] == "memory"
    assert boot_steps["database"]["status"] == "ok"
    assert boot_steps["system_files"]["status"] == "ok"
    assert boot_steps["memory"]["status"] == "error"
    assert boot_steps["memory_review"]["status"] == "not_run"
    assert boot_steps["conversation"]["status"] == "not_run"
    assert boot_steps["runtime"]["status"] == "not_run"


@pytest.mark.asyncio
async def test_create_app_runtime_cleans_up_on_start_failure(tmp_path, monkeypatch):
    stopped = []

    class FakeMCPManager:
        def __init__(self, configs, *, env_values=None, **kwargs):
            self.configs = configs

        async def start(self):
            return None

        async def stop(self):
            stopped.append(True)

    monkeypatch.setattr("personal_agent.mcp.manager.MCPManager", FakeMCPManager)
    async def fail_memory(*args, **kwargs):
        raise RuntimeError("memory bootstrap failed")

    monkeypatch.setattr("personal_agent.runtime.create_memory_manager", fail_memory)
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[],
        plugins_disabled=[],
        mcp_enabled=True,
        mcp_servers=[{"name": "config", "command": "python", "args": [], "enabled": True}],
        memory_external_provider="none",
    )

    with pytest.raises(RuntimeError, match="memory bootstrap failed"):
        await create_app_runtime(settings)

    assert stopped == [True]


@pytest.mark.asyncio
async def test_create_app_runtime_reports_mcp_boot_step(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    _write_mcp_plugin(plugins_dir / "mcp")

    class FakeMCPManager:
        def __init__(self, configs, *, env_values=None, **kwargs):
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
        plugins_enabled=["user/mcp"],
        plugins_disabled=[],
        mcp_enabled=True,
        memory_external_provider="none",
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
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[],
        plugins_disabled=[],
        mcp_enabled=False,
        memory_external_provider="none",
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
    monkeypatch.setenv("REMOTE_MCP_TOKEN", "resolved-secret")
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[plugins_dir],
        plugins_enabled=["user/mcp"],
        mcp_enabled=True,
        mcp_servers=[{
            "name": "config",
            "command": "python",
            "args": [],
            "headers_env": {"Authorization": "REMOTE_MCP_TOKEN"},
            "enabled": True,
        }],
    )

    from personal_agent.plugins.core.manager import PluginManager

    plugin_manager = PluginManager(settings)
    plugin_manager.discover()
    plugin_manager.load_enabled()

    created = {}

    class FakeMCPManager:
        def __init__(self, configs, *, env_values=None, **kwargs):
            created["configs"] = configs
            created["env_values"] = env_values
            self.started = False

        async def start(self):
            self.started = True

    monkeypatch.setattr("personal_agent.mcp.manager.MCPManager", FakeMCPManager)

    manager = await start_mcp_manager(settings, plugin_manager)

    assert manager.started
    assert [item.name if hasattr(item, "name") else item["name"] for item in created["configs"]] == ["config", "demo"]
    assert created["env_values"] == {"REMOTE_MCP_TOKEN": "resolved-secret"}
