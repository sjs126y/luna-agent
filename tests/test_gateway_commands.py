"""Gateway adapter for shared slash commands."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from luna_agent.config import Settings
from luna_agent.db.database import Database
from luna_agent.platforms.core import BasePlatformAdapter, ChatInfo, PlatformEntry, SendResult, platform_registry
from luna_agent.gateway.gateway import Gateway
from luna_agent.gateway.state import PlatformRuntime
from luna_agent.memory.manager import MemoryManager
from luna_agent.models.messages import (
    MessageEvent,
    MessagePart,
    OutboundMessage,
    PlatformCapabilities,
    SessionSource,
)
from luna_agent.plugins.models import CommandEntry
from luna_agent.conversation import ConversationTurnResult
from luna_agent.conversation import ConversationCoordinator, SubmissionOutcome, SubmissionStatus
from luna_agent.delivery import DeliveryOutbox, DeliveryService, PlatformDirectory


class Memory:
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
        self.active_start_count = 0
        self.active_stop_count = 0

    def get_command(self, name, *, scope="slash"):
        entry = self.commands.get(name)
        if entry is None:
            return None
        if entry.scope not in {scope, "both"}:
            return None
        return entry

    async def execute_command(self, name, **kwargs):
        value = self.commands[name].handler(**kwargs)
        if hasattr(value, "__await__"):
            value = await value
        return value

    async def invoke_hook(self, name, *args, **kwargs):
        return args[0] if args else None

    def list_plugins(self):
        return []

    async def start_active_plugins(self):
        self.active_start_count += 1

    async def stop_active_plugins(self):
        self.active_stop_count += 1


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
    platforms = PlatformDirectory()
    delivery = DeliveryService(
        sessions=gw._session_router,
        platforms=platforms,
        hook_manager=gw.hook_manager,
        outbox=DeliveryOutbox(db),
    )

    async def dispatch_command(request):
        from luna_agent.commands.runtime import CommandResult, handle_slash_command

        if request.command_runtime is None:
            return CommandResult.unhandled()
        return await handle_slash_command(request.command_runtime, request.input.text)

    coordinator = ConversationCoordinator(
        gw._conversation_service,
        command_dispatcher=dispatch_command,
        delivery_service=delivery,
    )
    gw._conversation_coordinator = coordinator
    gw._platform_directory = platforms
    gw._delivery_service = delivery
    yield gw
    coordinator.active_turns.cancel(None)
    await coordinator.close(cancel_pending=True)
    await db.close()


def _event(
    text: str,
    *,
    platform: str = "telegram",
    user_id: str = "u1",
    chat_id: str = "c1",
    message_id: str | None = None,
) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=platform,
            user_id=user_id,
            chat_id=chat_id,
            user_name="User",
        ),
        message_id=message_id,
    )


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    assert predicate()


def _attach_adapter(gateway, adapter, *, platform: str = "telegram"):
    adapter.mark_connected(name=platform)
    gateway._adapters.append(adapter)
    gateway._platform_directory.register(platform, adapter)
    return adapter


@pytest.mark.asyncio
async def test_gateway_session_command_uses_shared_service(gateway):
    event = _event("/session work")

    result = await gateway._handle_command(event, "telegram:c1:u1")

    assert result == "会话已切换: telegram:work:u1"
    listed = await gateway._handle_command(_event("/session list"), "telegram:work:u1")
    assert "telegram:work:u1" in listed


@pytest.mark.asyncio
async def test_gateway_message_inner_backfills_envelope_before_commands(gateway):
    event = _event("/session list")

    result = await gateway._handle_message_inner(event)

    assert result is None
    assert event.envelope is not None
    assert event.envelope.text == "/session list"
    assert event.envelope.source.chat_id == "c1"


@pytest.mark.asyncio
async def test_gateway_regular_message_uses_active_session_key(gateway, monkeypatch):
    await gateway._handle_command(_event("/session work"), "telegram:c1:u1")
    captured = []

    async def run_turn_input_events(session_key, user_input, **kwargs):
        captured.append((session_key, user_input.text))
        return ConversationTurnResult(
            final_response="ok",
            messages=[],
            completed=True,
            context_overflow=False,
            was_compressed=False,
            should_review_memory=False,
            raw={},
        )

    monkeypatch.setattr(gateway._conversation_service, "run_turn_input_events", run_turn_input_events)
    monkeypatch.setattr(gateway._auth_manager, "check", lambda user_id, text: (True, None))

    result = await gateway._handle_message_inner(_event("hello"))

    assert result is None
    assert captured == [("telegram:work:u1", "hello")]


@pytest.mark.asyncio
async def test_gateway_submits_normalized_request_when_coordinator_is_available(gateway, monkeypatch):
    captured = []

    class Handle:
        async def outcome(self):
            return SubmissionOutcome(
                request_id="sub-1",
                session_key="telegram:c1:u1",
                status=SubmissionStatus.COMPLETED,
                response="ok",
            )

    class Coordinator:
        async def submit(self, request):
            captured.append(request)
            return Handle()

    gateway._conversation_coordinator = Coordinator()
    monkeypatch.setattr(gateway._auth_manager, "check", lambda user_id, text: (True, None))

    result = await gateway._handle_message_inner(_event("hello", message_id="m1"))

    assert result is None
    assert captured[0].session_key == "telegram:c1:u1"
    assert captured[0].input.text == "hello"
    assert captured[0].response_mode.value == "deliver"
    assert captured[0].metadata == {"message_id": "m1"}


@pytest.mark.asyncio
async def test_coordinator_gateway_delivers_auth_response_as_protected_message(gateway, monkeypatch):
    deliveries = []

    class Delivery:
        async def deliver(self, request):
            deliveries.append(request)
            return SimpleNamespace(delivered=True)

    gateway._conversation_coordinator = SimpleNamespace()
    gateway._delivery_service = Delivery()
    monkeypatch.setattr(gateway._auth_manager, "check", lambda user_id, text: (False, "pair first"))

    result = await gateway._handle_message_inner(_event("hello"))

    assert result is None
    assert deliveries[0].kind.value == "auth"
    assert deliveries[0].message.render_text() == "pair first"


@pytest.mark.asyncio
async def test_gateway_message_hook_runs_after_auth_and_rewrites_input(gateway, monkeypatch):
    from luna_agent.hooks import GatewayMessageOutcome, HookEvent, HookManager

    hook_manager = HookManager()
    gateway.hook_manager = hook_manager
    captured = {}
    auth_calls = []

    async def rewrite(event):
        assert auth_calls == [("u1", "hello")]
        return GatewayMessageOutcome.replace_message(
            text="rewritten",
            metadata={"hooked": True},
        )

    async def run_turn_input_events(session_key, user_input, **kwargs):
        captured["text"] = user_input.text
        captured["metadata"] = user_input.metadata
        return ConversationTurnResult(
            final_response="ok",
            messages=[],
            completed=True,
            context_overflow=False,
            was_compressed=False,
            should_review_memory=False,
            raw={},
        )

    hook_manager.register(
        owner="test",
        event=HookEvent.GATEWAY_MESSAGE_RECEIVED,
        callback=rewrite,
        matcher="telegram",
    )
    monkeypatch.setattr(
        gateway._auth_manager,
        "check",
        lambda user_id, text: auth_calls.append((user_id, text)) or (True, None),
    )
    monkeypatch.setattr(gateway._conversation_service, "run_turn_input_events", run_turn_input_events)

    result = await gateway._handle_message_inner(_event("hello"))

    assert result is None
    assert captured == {"text": "rewritten", "metadata": {"hooked": True}}


@pytest.mark.asyncio
async def test_gateway_message_hook_can_block_before_agent(gateway, monkeypatch):
    from luna_agent.hooks import GatewayMessageOutcome, HookEvent, HookManager

    hook_manager = HookManager()
    gateway.hook_manager = hook_manager
    called = False

    async def block(event):
        return GatewayMessageOutcome.block("message rejected")

    async def run_turn_input_events(session_key, user_input, **kwargs):
        nonlocal called
        called = True

    hook_manager.register(
        owner="test",
        event=HookEvent.GATEWAY_MESSAGE_RECEIVED,
        callback=block,
    )
    monkeypatch.setattr(gateway._auth_manager, "check", lambda user_id, text: (True, None))
    monkeypatch.setattr(gateway._conversation_service, "run_turn_input_events", run_turn_input_events)

    result = await gateway._handle_message_inner(_event("hello"))

    assert result is None
    assert called is False


@pytest.mark.asyncio
async def test_gateway_prepares_inbound_attachments_after_auth(gateway, monkeypatch):
    adapter = _attach_adapter(gateway, FakeAdapter(gateway.config, gateway.db))
    prepared = []
    captured = {}

    async def prepare_inbound_attachments(event):
        prepared.append(event.text)
        event.envelope.metadata["prepared"] = True
        return event

    async def run_turn_input_events(session_key, user_input, **kwargs):
        captured["metadata"] = user_input.metadata
        return ConversationTurnResult(
            final_response="ok",
            messages=[],
            completed=True,
            context_overflow=False,
            was_compressed=False,
            should_review_memory=False,
            raw={},
        )

    monkeypatch.setattr(adapter, "prepare_inbound_attachments", prepare_inbound_attachments)
    monkeypatch.setattr(gateway._auth_manager, "check", lambda user_id, text: (True, None))
    monkeypatch.setattr(gateway._conversation_service, "run_turn_input_events", run_turn_input_events)

    event = _event("hello")
    event.attachments = [MessagePart(type="image", file_id="file-1", name="photo.png")]
    result = await gateway._handle_message_inner(event)

    assert result is None
    assert prepared == ["hello"]
    assert captured["metadata"]["prepared"] is True


@pytest.mark.asyncio
async def test_gateway_does_not_prepare_attachments_when_auth_fails(gateway, monkeypatch):
    adapter = _attach_adapter(gateway, FakeAdapter(gateway.config, gateway.db))
    prepared = []

    async def prepare_inbound_attachments(event):
        prepared.append(event.text)
        return event

    monkeypatch.setattr(adapter, "prepare_inbound_attachments", prepare_inbound_attachments)
    monkeypatch.setattr(gateway._auth_manager, "check", lambda user_id, text: (False, "denied"))

    event = _event("hello")
    event.attachments = [MessagePart(type="image", file_id="file-1", name="photo.png")]
    result = await gateway._handle_message_inner(event)

    assert result is None
    assert prepared == []


@pytest.mark.asyncio
async def test_gateway_does_not_prepare_attachments_for_consumed_command(gateway, monkeypatch):
    adapter = _attach_adapter(gateway, FakeAdapter(gateway.config, gateway.db))
    prepared = []

    async def prepare_inbound_attachments(event):
        prepared.append(event.text)
        return event

    monkeypatch.setattr(adapter, "prepare_inbound_attachments", prepare_inbound_attachments)
    monkeypatch.setattr(gateway._auth_manager, "check", lambda user_id, text: (True, None))

    result = await gateway._handle_message_inner(_event("/session list"))

    assert result is None
    assert prepared == []


@pytest.mark.asyncio
async def test_gateway_session_current_rename_and_delete(gateway):
    await gateway._session_store.get_or_create("telegram:c1:u1", _event("hello").source)
    gateway._agent_cache["telegram:c1:u1"] = Agent()

    current = await gateway._handle_command(_event("/session current"), "telegram:c1:u1")
    renamed = await gateway._handle_command(_event("/session rename renamed"), "telegram:c1:u1")
    listed = await gateway._handle_command(_event("/session list"), "telegram:renamed:u1")
    deleted = await gateway._handle_command(_event("/session delete current"), "telegram:renamed:u1")

    assert "session id" in current
    assert "telegram:renamed:u1" in renamed
    assert "telegram:renamed:u1" in listed
    assert "会话已删除: telegram:renamed:u1" in deleted
    assert "telegram:renamed:u1" not in gateway._agent_cache
    assert gateway._session_override.get("telegram:c1:u1") is None


@pytest.mark.asyncio
async def test_gateway_delete_named_session_without_switching(gateway):
    await gateway._handle_command(_event("/session work"), "telegram:c1:u1")
    gateway._agent_cache["telegram:work:u1"] = Agent()

    result = await gateway._handle_command(_event("/session delete work"), "telegram:c1:u1")

    assert "会话已删除: telegram:work:u1" in result
    assert "telegram:work:u1" not in gateway._agent_cache
    assert gateway._session_store.get("telegram:work:u1") is None


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


@pytest.mark.asyncio
async def test_gateway_does_not_run_cli_only_plugin_command(gateway):
    async def handler(args="", **kwargs):
        return "should-not-run"

    gateway.plugin_manager.commands["local"] = CommandEntry(
        name="local",
        description="local only",
        handler=handler,
        scope="cli",
    )

    result = await gateway._handle_command(_event("/local hi"), "telegram:c1:u1")

    assert result is None


@pytest.mark.asyncio
async def test_gateway_help_lists_slash_plugin_commands_only(gateway):
    async def handler(args="", **kwargs):
        return "ok"

    gateway.plugin_manager.commands["demo"] = CommandEntry(
        name="demo",
        description="gateway command",
        handler=handler,
        scope="slash",
        plugin_key="user/demo",
    )
    gateway.plugin_manager.commands["local"] = CommandEntry(
        name="local",
        description="local only",
        handler=handler,
        scope="cli",
        plugin_key="user/local",
    )

    result = await gateway._handle_command(_event("/help"), "telegram:c1:u1")

    assert "/demo - gateway command (user/demo)" in result
    assert "/local" not in result


@pytest.mark.asyncio
async def test_gateway_commands_lists_slash_plugin_commands_only(gateway):
    async def handler(args="", **kwargs):
        return "ok"

    gateway.plugin_manager.commands["demo"] = CommandEntry(
        name="demo",
        description="gateway command",
        handler=handler,
        scope="slash",
        plugin_key="user/demo",
    )
    gateway.plugin_manager.commands["local"] = CommandEntry(
        name="local",
        description="local only",
        handler=handler,
        scope="cli",
        plugin_key="user/local",
    )

    text = await gateway._handle_command(_event("/commands"), "telegram:c1:u1")
    data = await gateway._handle_command(_event("/commands json"), "telegram:c1:u1")

    assert "/commands - 列出 slash commands" in text
    assert "/demo - gateway command (user/demo)" in text
    assert "/local" not in text
    assert '"name": "demo"' in data
    assert '"name": "local"' not in data
    assert '"available_in": [' in data
    assert '"arguments": [' in data
    assert '"provider": "tools"' in data


@pytest.mark.asyncio
async def test_gateway_lifecycle_hooks_observe_start_connect_disconnect_stop(gateway, monkeypatch):
    from luna_agent.hooks import HookEvent, HookManager

    hook_manager = HookManager()
    gateway.hook_manager = hook_manager
    seen = []

    async def observe(event):
        seen.append((event.event_name.value, event.source.platform if event.source else ""))

    for event_name in (
        HookEvent.GATEWAY_START,
        HookEvent.PLATFORM_CONNECTED,
        HookEvent.GATEWAY_STOP,
        HookEvent.PLATFORM_DISCONNECTED,
    ):
        hook_manager.register(owner="test", event=event_name, callback=observe)

    monkeypatch.setattr(platform_registry, "_entries", {
        "demo": PlatformEntry(
            "demo",
            lambda config, db: FakeAdapter(config, db),
            lambda config: True,
        ),
    })

    await gateway.start()
    await gateway.stop()

    assert gateway.plugin_manager.active_start_count == 1
    assert gateway.plugin_manager.active_stop_count == 1
    assert seen == [
        ("PlatformConnected", "demo"),
        ("GatewayStart", ""),
        ("GatewayStop", ""),
        ("PlatformDisconnected", "demo"),
    ]


@pytest.mark.asyncio
async def test_gateway_async_confirmation_allows_once(gateway, monkeypatch):
    adapter = _attach_adapter(gateway, RecordingAdapter(gateway.config, gateway.db))
    monkeypatch.setattr(gateway._auth_manager, "check", lambda user_id, text: (True, None))

    answers = []

    async def run_turn_input_events(session_key, user_input, *, confirm=None, **kwargs):
        answer = await confirm(SimpleNamespace(
            tool_name="web_search",
            display_name="Web search",
            permission_category="network",
            input_preview="GPT-5.5",
            risk_summary="May access the network.",
        ))
        answers.append(answer)
        return ConversationTurnResult(
            final_response=f"answer:{answer}",
            messages=[],
            completed=True,
            context_overflow=False,
            was_compressed=False,
            should_review_memory=False,
            raw={},
        )

    monkeypatch.setattr(gateway._conversation_service, "run_turn_input_events", run_turn_input_events)

    task = asyncio.create_task(gateway._handle_message_inner(_event("search")))
    await _wait_until(lambda: adapter.sent_contents)

    assert "需要授权工具调用" in adapter.sent_contents[0]
    assert "回复 1 允许一次 / 2 拒绝 / 3 1小时允许" in adapter.sent_contents[0]
    assert gateway.health_snapshot()["pending_confirmation_count"] == 1

    invalid = await gateway._handle_message_inner(_event("hello"))
    ack = await gateway._handle_message_inner(_event("1"))
    final = await asyncio.wait_for(task, timeout=1)

    assert invalid is None
    assert ack is None
    assert final is None
    assert answers == ["allow"]
    assert "请回复 1、2 或 3；发送 /stop 可取消。" in adapter.sent_contents
    assert "已允许一次，继续执行。" in adapter.sent_contents
    assert "answer:allow" in adapter.sent_contents
    assert gateway.health_snapshot()["pending_confirmation_count"] == 0


@pytest.mark.asyncio
async def test_gateway_async_confirmation_accepts_cached_choice(gateway, monkeypatch):
    adapter = _attach_adapter(gateway, RecordingAdapter(gateway.config, gateway.db))
    monkeypatch.setattr(gateway._auth_manager, "check", lambda user_id, text: (True, None))

    async def run_turn_input_events(session_key, user_input, *, confirm=None, **kwargs):
        answer = await confirm(SimpleNamespace(
            tool_name="bash",
            display_name="Shell command",
            permission_category="bash",
            input_preview="date",
        ))
        return ConversationTurnResult(
            final_response=f"answer:{answer}",
            messages=[],
            completed=True,
            context_overflow=False,
            was_compressed=False,
            should_review_memory=False,
            raw={},
        )

    monkeypatch.setattr(gateway._conversation_service, "run_turn_input_events", run_turn_input_events)

    task = asyncio.create_task(gateway._handle_message_inner(_event("run")))
    await _wait_until(lambda: adapter.sent_contents)
    ack = await gateway._handle_message_inner(_event("3"))
    final = await asyncio.wait_for(task, timeout=1)

    assert ack is None
    assert final is None
    assert "已允许，1小时内同类工具不再询问。" in adapter.sent_contents
    assert "answer:always" in adapter.sent_contents

@pytest.mark.asyncio
async def test_platform_confirmation_reply_reaches_coordinator_control_lane(gateway, monkeypatch):
    adapter = _attach_adapter(gateway, RecordingAdapter(gateway.config, gateway.db))
    adapter.set_message_handler(gateway._handle_message)
    monkeypatch.setattr(gateway._auth_manager, "check", lambda user_id, text: (True, None))

    async def run_turn_input_events(session_key, user_input, *, confirm=None, **kwargs):
        answer = await confirm(SimpleNamespace(
            tool_name="web_search",
            display_name="Web search",
            permission_category="network",
            input_preview="GPT-5.5",
        ))
        return ConversationTurnResult(
            final_response=f"answer:{answer}",
            messages=[],
            completed=True,
            context_overflow=False,
            was_compressed=False,
            should_review_memory=False,
            raw={},
        )

    monkeypatch.setattr(gateway._conversation_service, "run_turn_input_events", run_turn_input_events)

    adapter.handle_message(_event("search", message_id="m1"))
    await _wait_until(lambda: adapter.sent_contents and "需要授权工具调用" in adapter.sent_contents[0])

    assert gateway.health_snapshot()["pending_confirmation_count"] == 1

    adapter.handle_message(_event("1", message_id="m2"))

    await _wait_until(
        lambda: "已允许一次，继续执行。" in adapter.sent_contents
        and "answer:allow" in adapter.sent_contents,
    )

    assert gateway.health_snapshot()["pending_confirmation_count"] == 0


@pytest.mark.asyncio
async def test_gateway_steer_command_queues_current_turn(gateway):
    session_key = "telegram:c1:u1"
    gateway._conversation_coordinator.active_turns.begin_turn(session_key, "turn-1")

    try:
        response = await gateway._handle_command(_event("/steer 回答短一点"), session_key)

        assert "已收到" in response
        snapshot = gateway._conversation_coordinator.active_turns.snapshot(session_key)
        assert snapshot["active_turn_id"] == "turn-1"
        assert snapshot["pending_count"] == 1
        assert snapshot["pending_items"][0]["turn_id"] == "turn-1"
        assert snapshot["pending_items"][0]["text_preview"] == "回答短一点"
        health = gateway.health_snapshot()
        assert health["pending_steer_count"] == 1
    finally:
        gateway._conversation_coordinator.active_turns.end_turn(session_key, "turn-1")


@pytest.mark.asyncio
async def test_platform_steer_reply_reaches_coordinator_control_lane(gateway, monkeypatch):
    adapter = _attach_adapter(gateway, RecordingAdapter(gateway.config, gateway.db))
    adapter.set_message_handler(gateway._handle_message)
    monkeypatch.setattr(gateway._auth_manager, "check", lambda user_id, text: (True, None))

    session_key = "telegram:c1:u1"
    gateway._conversation_coordinator.active_turns.begin_turn(session_key, "turn-1")

    try:
        adapter.handle_message(_event("/steer 回答短一点", message_id="steer-1"))
        await _wait_until(lambda: adapter.sent_contents)

        assert "已收到" in adapter.sent_contents[0]
        snapshot = gateway._conversation_coordinator.active_turns.snapshot(session_key)
        assert snapshot["pending_count"] == 1
        assert snapshot["pending_items"][0]["text_preview"] == "回答短一点"
    finally:
        gateway._conversation_coordinator.active_turns.end_turn(session_key, "turn-1")


@pytest.mark.asyncio
async def test_gateway_start_records_platform_health(gateway, monkeypatch):
    class GoodAdapter(FakeAdapter):
        async def connect(self) -> None:
            self.connected_called = True

    class BadAdapter(FakeAdapter):
        async def connect(self) -> None:
            raise RuntimeError("connect boom")

    monkeypatch.setattr(platform_registry, "_entries", {
        "good": PlatformEntry("good", lambda config, db: GoodAdapter(config, db), lambda config: True),
        "bad": PlatformEntry("bad", lambda config, db: BadAdapter(config, db), lambda config: True),
        "skip": PlatformEntry("skip", lambda config, db: GoodAdapter(config, db), lambda config: False),
    })

    await gateway.start()
    health = gateway.health_snapshot()
    platforms = {item["name"]: item for item in health["platforms"]}

    assert platforms["good"]["connected"] is True
    assert platforms["good"]["status"] == "connected"
    assert platforms["bad"]["last_connect_error"] == "RuntimeError: connect boom"
    assert platforms["bad"]["status"] == "reconnecting"
    assert platforms["bad"]["attempts"] == 1
    assert platforms["bad"]["next_retry_at"]
    assert platforms["skip"]["skipped_reason"] == "check_fn returned False"
    assert platforms["skip"]["status"] == "skipped"
    assert health["adapter_count"] == 1

    await gateway.stop()
    stopped = {item["name"]: item for item in gateway.health_snapshot()["platforms"]}
    assert stopped["good"]["connected"] is False


@pytest.mark.asyncio
async def test_base_adapter_health_records_send_failure(gateway):
    adapter = FailingSendAdapter(gateway.config, gateway.db)

    result = await adapter.send_message("chat", OutboundMessage.text("hello"))

    assert result.success is False
    health = adapter.health_snapshot()
    assert health["last_send_error"] == "send failed"
    assert health["send_stats"]["failed_count"] == 1
    assert health["send_stats"]["last_error"] == "send failed"


@pytest.mark.asyncio
async def test_base_adapter_health_has_no_conversation_queue_state(gateway):
    adapter = FakeAdapter(gateway.config, gateway.db)
    handled = []

    async def handler(event):
        handled.append(event.text)

    adapter.set_message_handler(handler)
    adapter.handle_message(_event("hello"))
    await _wait_until(lambda: handled == ["hello"])

    health = adapter.health_snapshot()

    assert "active_sessions" not in health
    assert "pending_messages" not in health
    assert health["last_message_at"]


@pytest.mark.asyncio
async def test_base_adapter_handler_failure_does_not_block_other_events(gateway):
    adapter = RecordingAdapter(gateway.config, gateway.db)
    calls = []

    async def handler(event):
        calls.append(event.text)
        if event.text == "first":
            raise RuntimeError("handler boom")

    adapter.set_message_handler(handler)
    adapter.handle_message(_event("first", message_id="m1"))
    adapter.handle_message(_event("second", message_id="m2"))

    await _wait_until(lambda: calls == ["first", "second"])

    assert calls == ["first", "second"]


@pytest.mark.asyncio
async def test_base_adapter_typing_failure_does_not_block_message(gateway):
    adapter = TypingFailAdapter(gateway.config, gateway.db)

    async def handler(event):
        handled.append(event.text)

    handled = []
    adapter.set_message_handler(handler)
    adapter.handle_message(_event("hello", message_id="m1"))

    await _wait_until(lambda: handled == ["hello"])


@pytest.mark.asyncio
async def test_base_adapter_does_not_send_handler_return_values(gateway):
    adapter = AlwaysFailSendAdapter(gateway.config, gateway.db)
    handled = []

    async def handler(event):
        handled.append(event.text)
        return f"reply:{event.text}"

    adapter.set_message_handler(handler)
    adapter.handle_message(_event("first", message_id="m1"))
    adapter.handle_message(_event("second", message_id="m2"))

    await _wait_until(lambda: handled == ["first", "second"])

    assert adapter.send_attempts == 0


@pytest.mark.asyncio
async def test_base_adapter_does_not_retry_format_errors(gateway):
    adapter = SequenceSendAdapter(gateway.config, gateway.db, [
        SendResult(success=False, error="markdown parse error"),
        SendResult(success=True, message_id="ok"),
    ])

    result = await adapter.send_message(
        "chat",
        OutboundMessage.text("**hello** `code` [link](https://example.test)"),
    )

    assert result.success is False
    assert adapter.sent_contents == ["**hello** `code` [link](https://example.test)"]
    assert adapter.sleep_delays == []
    health = adapter.health_snapshot()
    assert health["send_stats"]["retry_count"] == 0
    assert health["send_stats"]["failed_count"] == 1


@pytest.mark.asyncio
async def test_base_adapter_splits_outbound_text_by_platform_limit(gateway):
    adapter = TinyLimitAdapter(gateway.config, gateway.db)

    result = await adapter.send_message("chat", OutboundMessage.text("abcdefghijklmnop"))

    assert result.success is True
    assert adapter.sent_contents == ["abcde", "fghij", "klmno", "p"]
    health = adapter.health_snapshot()
    assert health["send_stats"]["sent_count"] == 4
    assert health["send_stats"]["failed_count"] == 0


@pytest.mark.asyncio
async def test_base_adapter_marks_failed_later_chunk_as_partial_delivery(gateway):
    adapter = SequenceSendAdapter(gateway.config, gateway.db, [
        SendResult(success=True, message_id="first"),
        SendResult(success=False, error="second failed"),
    ])
    adapter.capabilities = PlatformCapabilities(max_text_length=5)

    result = await adapter.send_message("chat", OutboundMessage.text("abcdefghij"))

    assert result.success is False
    assert result.error == "partial delivery: second failed"
    assert adapter.sent_contents == ["abcde", "fghij"]


@pytest.mark.asyncio
async def test_base_adapter_timeout_send_error_does_not_retry(gateway):
    adapter = SequenceSendAdapter(gateway.config, gateway.db, [
        SendResult(success=False, error="request timeout"),
    ])

    result = await adapter.send_message("chat", OutboundMessage.text("hello"))

    assert result.success is False
    assert adapter.sent_contents == ["hello"]
    health = adapter.health_snapshot()
    assert health["send_stats"]["retry_count"] == 0
    assert health["send_stats"]["failed_count"] == 1
    assert health["last_send_error"] == "request timeout"


@pytest.mark.asyncio
async def test_base_adapter_send_exception_is_not_retried(gateway):
    adapter = SequenceSendAdapter(gateway.config, gateway.db, [
        RuntimeError("temporary"),
        RuntimeError("temporary"),
        RuntimeError("temporary"),
    ])

    with pytest.raises(RuntimeError, match="temporary"):
        await adapter.send_message("chat", OutboundMessage.text("hello"))

    assert adapter.sent_contents == ["hello"]
    assert adapter.sleep_delays == []
    health = adapter.health_snapshot()
    assert health["send_stats"]["retry_count"] == 0
    assert health["send_stats"]["failed_count"] == 1
    assert "RuntimeError: temporary" in health["last_send_error"]


@pytest.mark.asyncio
async def test_base_adapter_deduplicates_same_message_id(gateway):
    adapter = RecordingAdapter(gateway.config, gateway.db)
    handled = []

    async def handler(event):
        handled.append(event.message_id)
        return f"ok:{event.message_id}"

    adapter.set_message_handler(handler)
    adapter.handle_message(_event("hello", message_id="same"))
    adapter.handle_message(_event("hello duplicate", message_id="same"))

    await _wait_until(lambda: handled == ["same"])

    health = adapter.health_snapshot()
    assert handled == ["same"]
    assert health["dedupe_size"] == 1


@pytest.mark.asyncio
async def test_base_adapter_dedupes_only_when_message_id_is_present(gateway):
    adapter = RecordingAdapter(gateway.config, gateway.db)
    handled = []

    async def handler(event):
        handled.append(event.text)
        return f"ok:{event.text}"

    adapter.set_message_handler(handler)
    adapter.handle_message(_event("first"))
    adapter.handle_message(_event("second"))

    await _wait_until(lambda: handled == ["first", "second"])

    assert handled == ["first", "second"]
    assert adapter.health_snapshot()["dedupe_size"] == 0


@pytest.mark.asyncio
async def test_base_adapter_dedupe_key_includes_platform_chat_and_user(gateway):
    adapter = RecordingAdapter(gateway.config, gateway.db)
    handled = []

    async def handler(event):
        handled.append((
            event.source.platform,
            event.source.chat_id,
            event.source.user_id,
            event.message_id,
        ))
        return "ok"

    adapter.set_message_handler(handler)
    adapter.handle_message(_event("a", platform="telegram", chat_id="c1", user_id="u1", message_id="same"))
    adapter.handle_message(_event("b", platform="telegram", chat_id="c2", user_id="u1", message_id="same"))
    adapter.handle_message(_event("c", platform="telegram", chat_id="c1", user_id="u2", message_id="same"))
    adapter.handle_message(_event("d", platform="feishu", chat_id="c1", user_id="u1", message_id="same"))

    await _wait_until(lambda: len(handled) == 4)

    assert len(handled) == 4
    assert adapter.health_snapshot()["dedupe_size"] == 4


@pytest.mark.asyncio
async def test_base_adapter_dedupe_lru_evicts_old_keys(gateway):
    adapter = RecordingAdapter(gateway.config, gateway.db)
    adapter._dedupe_max_size = 2
    handled = []

    async def handler(event):
        handled.append(event.message_id)
        return f"ok:{event.message_id}"

    adapter.set_message_handler(handler)
    adapter.handle_message(_event("one", message_id="m1"))
    adapter.handle_message(_event("two", message_id="m2"))
    adapter.handle_message(_event("three", message_id="m3"))
    adapter.handle_message(_event("one again", message_id="m1"))

    await _wait_until(lambda: len(handled) == 4)

    assert handled == ["m1", "m2", "m3", "m1"]
    health = adapter.health_snapshot()
    assert health["dedupe_size"] == 2
    assert health["dedupe_max_size"] == 2


def test_base_adapter_uses_gateway_settings(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        """
gateway:
  platform_reconnect_delays: [3, 7, 11]
  platform_message_dedupe_max_size: 6
storage:
  data_dir: ./data
sandbox:
  roots: [./data]
  bash_work_dir: ./data
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = Settings()
    adapter = RecordingAdapter(settings, object())

    assert adapter._dedupe_max_size == 6

    runtime = PlatformRuntime(name="demo", backoff_delays_seconds=tuple(settings.platform_reconnect_delays))
    runtime.attempts = 1
    assert runtime.next_retry_delay() == 3
    runtime.attempts = 3
    assert runtime.next_retry_delay() == 11


@pytest.mark.asyncio
async def test_base_adapter_does_not_serialize_same_session_messages(gateway):
    adapter = RecordingAdapter(gateway.config, gateway.db)
    handled = []

    async def handler(event):
        handled.append(f"start:{event.text}")
        await asyncio.sleep(0.02)
        handled.append(f"end:{event.text}")

    adapter.set_message_handler(handler)
    adapter.handle_message(_event("one", message_id="m1"))
    adapter.handle_message(_event("two", message_id="m2"))
    adapter.handle_message(_event("three", message_id="m3"))

    await _wait_until(lambda: len(handled) == 6)

    assert handled[:3] == ["start:one", "start:two", "start:three"]
    assert sorted(handled[3:]) == ["end:one", "end:three", "end:two"]


@pytest.mark.asyncio
async def test_base_adapter_different_sessions_can_run_concurrently(gateway):
    adapter = RecordingAdapter(gateway.config, gateway.db)
    started = []
    both_started = asyncio.Event()
    release = asyncio.Event()

    async def handler(event):
        started.append(event.source.chat_id)
        if len(started) == 2:
            both_started.set()
        await release.wait()

    adapter.set_message_handler(handler)
    adapter.handle_message(_event("one", chat_id="c1", message_id="m1"))
    adapter.handle_message(_event("two", chat_id="c2", message_id="m2"))

    await asyncio.wait_for(both_started.wait(), timeout=1)
    assert sorted(started) == ["c1", "c2"]

    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_gateway_health_uses_coordinator_state(gateway):
    gateway._conversation_coordinator.active_turns.begin_turn("telegram:c1:u1", "turn-1")
    health = gateway.health_snapshot()

    assert health["active_adapter_sessions"] == 0
    assert health["conversation_coordinator"]["active_count"] == 0
    assert health["steer"]["active_turn_count"] == 1
    gateway._conversation_coordinator.active_turns.end_turn("telegram:c1:u1", "turn-1")


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
        self._interrupt_requested = False


class FakeAdapter(BasePlatformAdapter):
    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id: str, content: str) -> SendResult:
        return SendResult(success=True, message_id="ok")

    async def get_chat_info(self, chat_id: str) -> ChatInfo:
        return ChatInfo(chat_id=chat_id)


class FailingSendAdapter(FakeAdapter):
    async def send(self, chat_id: str, content: str) -> SendResult:
        return SendResult(success=False, error="send failed")


class RecordingAdapter(FakeAdapter):
    def __init__(self, config, db):
        super().__init__(config, db)
        self.sent_contents = []
        self.sleep_delays = []

    async def send(self, chat_id: str, content: str) -> SendResult:
        self.sent_contents.append(content)
        return SendResult(success=True, message_id="ok")

    async def _sleep_before_retry(self, delay: float) -> None:
        self.sleep_delays.append(delay)


class TinyLimitAdapter(RecordingAdapter):
    capabilities = PlatformCapabilities(text=True, max_text_length=5)


class TypingFailAdapter(RecordingAdapter):
    async def _send_typing(self, chat_id: str) -> None:
        raise RuntimeError("typing boom")


class AlwaysFailSendAdapter(RecordingAdapter):
    def __init__(self, config, db):
        super().__init__(config, db)
        self.send_attempts = 0

    async def send(self, chat_id: str, content: str) -> SendResult:
        self.send_attempts += 1
        self.sent_contents.append(content)
        return SendResult(success=False, error="send failed")


class SequenceSendAdapter(RecordingAdapter):
    def __init__(self, config, db, outcomes):
        super().__init__(config, db)
        self.outcomes = list(outcomes)

    async def send(self, chat_id: str, content: str) -> SendResult:
        self.sent_contents.append(content)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_gateway_usage_does_not_create_agent(gateway):
    result = await gateway._handle_command(_event("/usage"), "telegram:c1:u1")

    assert result == "暂无会话数据。"
    assert gateway._agent_cache == {}


@pytest.mark.asyncio
async def test_gateway_removed_allow_is_unhandled_and_stop_applies_to_cached_agents(gateway):
    gateway._agent_cache["telegram:c1:u1"] = Agent()
    gateway._agent_cache["telegram:work:u1"] = Agent()

    allowed = await gateway._handle_command(_event("/allow write"), "telegram:c1:u1")
    stopped = await gateway._handle_command(_event("/stop"), "telegram:c1:u1")

    assert allowed is None
    assert stopped == "已停止。"
    assert all(agent._interrupt_requested for agent in gateway._agent_cache.values())


@pytest.mark.asyncio
async def test_gateway_stop_reports_delegate_agent_count(gateway, monkeypatch):
    gateway._agent_cache["telegram:c1:u1"] = Agent()
    monkeypatch.setattr(
        "luna_agent.plugins.builtin.tools.builtin.delegate.stop_delegate_agents",
        lambda: 3,
    )

    stopped = await gateway._handle_command(_event("/stop"), "telegram:c1:u1")

    assert stopped == "已停止。已请求停止 3 个子 agent。"
    assert all(agent._interrupt_requested for agent in gateway._agent_cache.values())
