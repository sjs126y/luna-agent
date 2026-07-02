"""Shared command runtime adapter behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_agent.conversation import ConversationCommandRuntime


class Agent:
    def __init__(self):
        self._destructive_allowed = set()


class Service:
    def __init__(self):
        self.agent_cache = {
            "cli:default:local": Agent(),
            "cli:other:local": Agent(),
        }
        self.usage_kwargs = None

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

    def allow_agent_category(self, session_key, category):
        agent = self.agent_cache.get(session_key)
        if agent is None:
            return False
        agent._destructive_allowed.add(category)
        return True

    def allow_all_cached_agents(self, category):
        for agent in self.agent_cache.values():
            agent._destructive_allowed.add(category)
        return len(self.agent_cache)

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


@pytest.mark.asyncio
async def test_conversation_command_runtime_allow_current_or_all_cached_agents():
    service = Service()
    runtime = Runtime(service)
    await runtime.allow_category("write")

    assert service.agent_cache["cli:default:local"]._destructive_allowed == {"write"}
    assert service.agent_cache["cli:other:local"]._destructive_allowed == set()

    class AllRuntime(Runtime):
        allow_all_cached_agents = True

    await AllRuntime(service).allow_category("bash")

    assert service.agent_cache["cli:default:local"]._destructive_allowed == {"write", "bash"}
    assert service.agent_cache["cli:other:local"]._destructive_allowed == {"bash"}
