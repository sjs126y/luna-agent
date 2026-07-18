"""Shared command runtime adapter behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_agent.conversation import ConversationCommandRuntime
from personal_agent.commands.runtime import handle_slash_command


class Service:
    def __init__(self):
        self.agent_cache = {
            "cli:default:local": SimpleNamespace(),
            "cli:other:local": SimpleNamespace(),
        }
        self.usage_kwargs = None
        from personal_agent.security.session import SecurityStateStore

        self.security_states = SecurityStateStore(SimpleNamespace(
            execution_mode="ask-first",
            sandbox_roots=[Path.cwd()],
            permission_grant_ttl_minutes=60,
            tool_approval_config={},
        ))

    async def get_or_create_agent(self, session_key):
        return self.agent_cache[session_key]

    async def reset_session(self, session_key, source):
        return "new-id"

    def clear_agent(self, session_key):
        self.agent_cache.pop(session_key, None)

    async def session_list_summary(self, **kwargs):
        return f"{kwargs['platform']}:{kwargs['user_id']}:{kwargs['current_key']}"

    async def current_session_summary(self, session_key, source):
        return f"current:{session_key}"

    async def load_history(self, session_key, source):
        return [{"role": "user", "content": "hello"}]

    def default_export_path(self, session_key):
        return Path("/tmp") / f"{session_key}.jsonl"

    async def export_session(self, session_key, source, export_path):
        return 1

    async def usage_summary(self, session_key, source, **kwargs):
        self.usage_kwargs = kwargs
        return "usage"

    def security_context(self, session_key):
        return self.security_states.context(session_key)

    def request_stop(self, session_key=None):
        return 2


class Runtime(ConversationCommandRuntime):
    def __init__(self, service):
        self.conversation_service = service
        self.settings = SimpleNamespace()
        self.plugin_manager = None

    @property
    def session_key(self):
        return "cli:default:local"

    @property
    def source(self):
        return SimpleNamespace(platform="cli", user_id="local")


@pytest.mark.asyncio
async def test_conversation_command_runtime_common_methods():
    service = Service()
    runtime = Runtime(service)

    assert await runtime.list_sessions() == "cli:local:cli:default:local"
    assert await runtime.current_session() == "current:cli:default:local"
    assert await runtime.load_history() == [{"role": "user", "content": "hello"}]
    assert await runtime.export_session() == (1, "/tmp/cli:default:local.jsonl")
    assert await runtime.usage(current_user_message="/usage") == "usage"
    assert service.usage_kwargs["create_agent"] is True
    assert await runtime.stop_agents() == "已停止。已请求停止 2 个子 agent。"
    activity = await runtime.activity_snapshot()
    assert activity["summary"]["active_total"] >= 0


@pytest.mark.asyncio
async def test_conversation_command_runtime_activity_uses_gateway_snapshot():
    service = Service()
    runtime = Runtime(service)
    runtime.gateway = SimpleNamespace(
        health_snapshot=lambda: {
            "running_agents": 1,
            "stop_requested_agents": 0,
            "running_agent_runs": [{
                "session_key": "telegram:c1:u1",
                "platform": "telegram",
                "chat_id": "c1",
                "user_id": "u1",
                "status": "running",
            }],
        }
    )

    snapshot = await runtime.activity_snapshot()
    detail = await runtime.activity_detail("gateway_agent", "telegram:c1:u1")
    choices = await runtime.slash_argument_choices(
        "activity_gateway",
        command="activity",
        args=("gateway",),
        query="telegram",
    )
    metadata = runtime.slash_command_metadata()

    assert snapshot["gateway_agents"]["running_agent_runs"][0]["id"] == "telegram:c1:u1"
    assert detail["gateway_run"]["platform"] == "telegram"
    assert choices[0]["value"] == "telegram:c1:u1"
    assert any(item["name"] == "activity" for item in metadata)


@pytest.mark.asyncio
async def test_conversation_command_runtime_clears_current_session_grants():
    service = Service()
    runtime = Runtime(service)
    current = service.security_context(runtime.session_key)
    other = service.security_context("cli:other:local")
    current.state.grant_tool("core:write", ttl_seconds=60)
    other.state.grant_tool("core:bash", ttl_seconds=60)

    assert await runtime.clear_security_grants() is True
    assert current.state.tool_grants == {}
    assert "core:bash" in other.state.tool_grants


@pytest.mark.asyncio
async def test_plugins_command_controls_live_runtime():
    runtime = Runtime(Service())
    plugin = SimpleNamespace(
        key="user/demo",
        status=SimpleNamespace(value="LOADED"),
        generation_id="user/demo@g2",
        runtime_instance_id="runtime-2",
        active_enabled=False,
        active_runner=None,
    )

    class Plugins:
        def __init__(self):
            self.queries = self

        def list_plugins(self):
            return [{
                "key": plugin.key,
                "status": "LOADED",
                "generation_id": plugin.generation_id,
            }]

        def plugin_info(self, key):
            return {"key": key, "status": "LOADED", "generation_id": plugin.generation_id}

        async def reload_plugin_runtime(self, key):
            assert key == plugin.key
            return plugin

        async def set_active_enabled(self, key, enabled):
            assert key == plugin.key
            plugin.active_enabled = enabled
            return plugin

        def runtime_health(self):
            return {"current_revision": 4, "active_leases": 0}

        def operations(self, *, key="", limit=50):
            return [{"operation_id": "pop_test", "plugin_key": key, "status": "completed"}]

    runtime.plugin_manager = Plugins()

    listed = await handle_slash_command(runtime, "/plugins list")
    reloaded = await handle_slash_command(runtime, "/plugins reload user/demo")
    active = await handle_slash_command(runtime, "/plugins active user/demo on")

    assert listed.handled and "user/demo" in listed.response
    assert reloaded.handled and reloaded.payload["generation_id"] == "user/demo@g2"
    assert reloaded.payload["operation_id"] == "pop_test"
    assert active.handled and active.payload["active_enabled"] is True
