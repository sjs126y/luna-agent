from __future__ import annotations

from types import SimpleNamespace

import pytest

from personal_agent.config import Settings
from personal_agent.conversation import (
    ConversationCoordinator,
    ConversationService,
    ResponseMode,
    SessionDirectory,
    SubmissionOrigin,
    SubmissionRequest,
    SubmissionStatus,
)
from personal_agent.db.database import Database
from personal_agent.delivery import DeliveryOutbox, DeliveryService, PlatformDirectory
from personal_agent.gateway.compression_chain import CompressionChain
from personal_agent.gateway.session_store import SessionStore
from personal_agent.models.messages import SessionSource
from personal_agent.platforms.core import SendResult
from personal_agent.tools.registry import tool_registry


class PluginManager:
    async def invoke_hook(self, name, *args, **kwargs):
        return None


class Agent:
    session_api_calls = 0
    session_prompt_tokens = 0
    session_completion_tokens = 0
    _last_skill_summaries = ""
    _last_skill_injection = ""
    _last_memory_injections = ""
    _tool_calls_this_turn = 0
    _max_tool_calls_per_turn = 40
    _cached_system_prompt = "system"
    _memory_manager = None
    tools = []
    model = "test-model"

    def __init__(self):
        self._tools_generation = tool_registry.generation
        self._interrupt_requested = False
        self._provider = SimpleNamespace(model="test-model", context_window=1000)


class Context:
    def __init__(self, messages, *, user_idx: int, turn_id: str):
        self.messages = messages
        self.current_turn_user_idx = user_idx
        self.turn_id = turn_id
        self.was_compressed = False
        self.should_review_memory = False
        self.hook_contexts = []


class Adapter:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id, message):
        self.sent.append((chat_id, message.render_text()))
        return SendResult(success=True, message_id=f"message-{len(self.sent)}")


async def _runtime(tmp_path):
    settings = Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
    db = Database(settings.agent_data_dir / "state.db")
    await db.initialize()
    chain = CompressionChain(settings.agent_data_dir / "compression_chain.json")
    chain.load()
    store = SessionStore(db, settings.agent_data_dir, chain=chain)
    await store.initialize()
    session_key = "wechat:c1:u1"
    service = ConversationService(
        settings=settings,
        plugin_manager=PluginManager(),
        session_store=store,
        compression_chain=chain,
        memory_manager=None,
        agent_cache={session_key: Agent()},
    )
    sessions = SessionDirectory()
    source = SessionSource(platform="wechat", user_id="u1", chat_id="c1")
    sessions.bind(session_key, source)
    platforms = PlatformDirectory()
    adapter = Adapter()
    platforms.register("wechat", adapter)
    outbox = DeliveryOutbox(db, max_attempts=3)
    delivery = DeliveryService(
        sessions=sessions,
        platforms=platforms,
        outbox=outbox,
    )
    coordinator = ConversationCoordinator(service, delivery_service=delivery)
    return SimpleNamespace(
        settings=settings,
        db=db,
        store=store,
        service=service,
        sessions=sessions,
        adapter=adapter,
        outbox=outbox,
        coordinator=coordinator,
        session_key=session_key,
        source=source,
    )


def _request(runtime, text: str) -> SubmissionRequest:
    return SubmissionRequest.text(
        session_key=runtime.session_key,
        text=text,
        origin=SubmissionOrigin.GATEWAY,
        response_mode=ResponseMode.DELIVER,
        source=runtime.source,
    )


@pytest.mark.asyncio
async def test_submission_persists_and_delivers_through_full_runtime(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)

    async def build_turn_context(agent, text, history, *, turn_id=""):
        messages = list(history) + [
            {"role": "user", "content": [{"type": "text", "text": text}]},
            {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
        ]
        return Context(messages, user_idx=len(history), turn_id=turn_id)

    async def run_conversation(agent, ctx, **kwargs):
        return {
            "final_response": "answer",
            "messages": ctx.messages,
            "completed": True,
            "status": "completed",
        }

    monkeypatch.setattr("personal_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("personal_agent.agent.loop.run_conversation", run_conversation)

    handle = await runtime.coordinator.submit(_request(runtime, "hello"))
    outcome = await handle.outcome()
    session = await runtime.store.get_or_create(runtime.session_key, runtime.source)
    history = await runtime.store.load_history(session.session_id)
    delivery = outcome.payload["delivery_result"]
    outbox_record = await runtime.outbox.get(delivery.delivery_id)

    assert outcome.status == SubmissionStatus.COMPLETED
    assert [item["content"][0]["text"] for item in history] == ["hello", "answer"]
    assert runtime.adapter.sent == [("c1", "answer")]
    assert outbox_record.status == "delivered"
    await runtime.coordinator.close()
    await runtime.db.close()


@pytest.mark.asyncio
async def test_stopped_submission_persists_completed_tool_and_delivers_stop(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)

    async def build_turn_context(agent, text, history, *, turn_id=""):
        messages = list(history) + [
            {"role": "user", "content": [{"type": "text", "text": text}]},
            {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "write_file",
                    "input": {"path": "result.txt"},
                }],
            },
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "written",
                }],
            },
        ]
        return Context(messages, user_idx=len(history), turn_id=turn_id)

    async def run_conversation(agent, ctx, **kwargs):
        return {
            "final_response": "已停止。",
            "messages": ctx.messages,
            "completed": False,
            "status": "stopped",
            "turn_report": {"status": "stopped"},
        }

    monkeypatch.setattr("personal_agent.agent.context.build_turn_context", build_turn_context)
    monkeypatch.setattr("personal_agent.agent.loop.run_conversation", run_conversation)

    handle = await runtime.coordinator.submit(_request(runtime, "write it"))
    outcome = await handle.outcome()
    session = await runtime.store.get_or_create(runtime.session_key, runtime.source)
    history = await runtime.store.load_history(session.session_id)
    turn_result = outcome.payload["turn_result"]

    assert outcome.status == SubmissionStatus.CANCELLED
    assert [item["content"][0]["text"] for item in history] == [
        "write it",
        "written",
        "已停止。",
    ]
    assert turn_result.turn_report["persistence"]["tool_calls_saved"] == 1
    assert runtime.adapter.sent == [("c1", "已停止。")]
    await runtime.coordinator.close()
    await runtime.db.close()
