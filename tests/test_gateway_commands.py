"""Gateway adapter for shared slash commands."""

from __future__ import annotations

import pytest
import pytest_asyncio

from personal_agent.config import Settings
from personal_agent.db.database import Database
from personal_agent.gateway.gateway import Gateway
from personal_agent.memory.base import MemoryProvider
from personal_agent.memory.manager import MemoryManager
from personal_agent.models.messages import MessageEvent, SessionSource
from personal_agent.plugins.models import CommandEntry


class Memory(MemoryProvider):
    async def prefetch(self, user_message: str) -> list[dict]:
        return []

    async def save(self, content: str) -> None:
        return None

    async def search(self, query: str) -> list[str]:
        return []

    async def load_all(self) -> list[str]:
        return []

    def get_system_prompt_text(self) -> str:
        return ""


class PluginManager:
    def __init__(self):
        self.commands = {}

    def get_command(self, name, *, scope="slash"):
        return self.commands.get(name)

    async def execute_command(self, name, **kwargs):
        value = self.commands[name].handler(**kwargs)
        if hasattr(value, "__await__"):
            value = await value
        return value


@pytest_asyncio.fixture
async def gateway(tmp_path):
    settings = Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
    db = Database(settings.agent_data_dir / "state.db")
    await db.initialize()
    gw = Gateway(
        settings,
        db,
        MemoryManager(Memory()),
        plugin_manager=PluginManager(),
    )
    gw._compression_chain.load()
    await gw._session_store.initialize()
    yield gw
    await db.close()


def _event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform="telegram",
            user_id="u1",
            chat_id="c1",
            user_name="User",
        ),
    )


@pytest.mark.asyncio
async def test_gateway_session_command_uses_shared_service(gateway):
    event = _event("/session work")

    result = await gateway._handle_command(event, "telegram:c1:u1")

    assert result == "会话已切换: telegram:work:u1"
    listed = await gateway._handle_command(_event("/session list"), "telegram:work:u1")
    assert "telegram:work:u1" in listed


@pytest.mark.asyncio
async def test_gateway_plugin_command_receives_gateway_kwargs(gateway):
    async def handler(args="", **kwargs):
        assert kwargs["event"].text == "/demo hi"
        assert kwargs["gateway"] is gateway
        return f"{args}:{kwargs['session_key']}"

    gateway.plugin_manager.commands["demo"] = CommandEntry(
        name="demo",
        description="demo",
        handler=handler,
    )

    result = await gateway._handle_command(_event("/demo hi"), "telegram:c1:u1")

    assert result == "hi:telegram:c1:u1"


class Agent:
    session_api_calls = 0
    session_prompt_tokens = 0
    session_completion_tokens = 0
    _last_skill_summaries = ""
    _last_skill_injection = ""
    _last_memory_injections = ""
    _tool_calls_this_turn = 0
    _max_tool_calls_per_turn = 20
    _cached_system_prompt = "system"
    tools = []
    model = "deepseek-chat"
    _memory_manager = None

    class Provider:
        model = "deepseek-chat"
        context_window = 1000

    _provider = Provider()

    def __init__(self):
        self._destructive_allowed = set()
        self._interrupt_requested = False


@pytest.mark.asyncio
async def test_gateway_usage_does_not_create_agent(gateway):
    result = await gateway._handle_command(_event("/usage"), "telegram:c1:u1")

    assert result == "暂无会话数据。"
    assert gateway._agent_cache == {}


@pytest.mark.asyncio
async def test_gateway_allow_and_stop_apply_to_cached_agents(gateway):
    gateway._agent_cache["telegram:c1:u1"] = Agent()
    gateway._agent_cache["telegram:work:u1"] = Agent()

    allowed = await gateway._handle_command(_event("/allow write"), "telegram:c1:u1")
    stopped = await gateway._handle_command(_event("/stop"), "telegram:c1:u1")

    assert "已授权 write" in allowed
    assert stopped == "已停止。"
    assert all("write" in agent._destructive_allowed for agent in gateway._agent_cache.values())
    assert all(agent._interrupt_requested for agent in gateway._agent_cache.values())
