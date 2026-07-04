"""Gateway adapter for shared slash commands."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from personal_agent.config import Settings
from personal_agent.db.database import Database
from personal_agent.platforms.core import BasePlatformAdapter, ChatInfo, PlatformEntry, SendResult, platform_registry
from personal_agent.gateway.gateway import Gateway
from personal_agent.gateway.state import PlatformRuntime
from personal_agent.memory.base import MemoryProvider
from personal_agent.memory.manager import MemoryManager
from personal_agent.models.messages import MessageEvent, SessionSource
from personal_agent.plugins.models import CommandEntry
from personal_agent.conversation import ConversationTurnResult


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

    assert result is not None
    assert event.envelope is not None
    assert event.envelope.text == "/session list"
    assert event.envelope.source.chat_id == "c1"


@pytest.mark.asyncio
async def test_gateway_regular_message_uses_active_session_key(gateway, monkeypatch):
    await gateway._handle_command(_event("/session work"), "telegram:c1:u1")
    captured = []

    async def run_turn(session_key, source, text):
        captured.append((session_key, text))
        return ConversationTurnResult(
            final_response="ok",
            messages=[],
            completed=True,
            context_overflow=False,
            was_compressed=False,
            should_review_memory=False,
            raw={},
        )

    monkeypatch.setattr(gateway._conversation_service, "run_turn", run_turn)
    monkeypatch.setattr(gateway._auth_manager, "check", lambda user_id, text: (True, None))

    result = await gateway._handle_message_inner(_event("hello"))

    assert result == "ok"
    assert captured == [("telegram:work:u1", "hello")]


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
async def test_gateway_before_send_and_memory_review_use_conversation_result(gateway, monkeypatch):
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "base"}]},
    ]
    gateway._conversation_service.agent_cache["telegram:c1:u1"] = Agent()

    async def run_turn(session_key, source, text):
        assert session_key == "telegram:c1:u1"
        assert text == "hello"
        return ConversationTurnResult(
            final_response="base",
            messages=messages,
            completed=True,
            context_overflow=False,
            was_compressed=False,
            should_review_memory=True,
            raw={},
        )

    async def before_send(text, source):
        return text + "!"

    captured = []
    gateway.hooks.on_before_send.append(before_send)
    monkeypatch.setattr(gateway._conversation_service, "run_turn", run_turn)
    monkeypatch.setattr(
        gateway._memory_review_service,
        "maybe_spawn",
        lambda **kwargs: captured.append(kwargs) or True,
    )

    result = await gateway._handle_message_with_agent(_event("hello"), "telegram:c1:u1")

    assert result == "base!"
    assert captured == [{
        "agent": gateway._conversation_service.agent_cache["telegram:c1:u1"],
        "messages": messages,
        "should_review": True,
        "final_response": "base!",
    }]


def test_gateway_health_snapshot_reports_runtime_state(gateway):
    gateway._run_state.begin("telegram:c1:u1", _event("hello").source)
    gateway._agent_cache["telegram:c1:u1"] = Agent()

    health = gateway.health_snapshot()

    assert health["running_agents"] == 1
    assert health["cached_agents"] == 1
    assert health["running_agent_sessions"] == ["telegram:c1:u1"]
    assert health["running_agent_runs"][0]["status"] == "running"
    assert health["longest_running_seconds"] >= 0


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

    await adapter._send_with_retry("chat", "hello", max_retries=0)

    health = adapter.health_snapshot()
    assert health["last_send_error"] == "send failed"
    assert health["send_stats"]["failed_count"] == 1
    assert health["send_stats"]["last_error"] == "send failed"


@pytest.mark.asyncio
async def test_base_adapter_health_records_pending_queue(gateway):
    adapter = FakeAdapter(gateway.config, gateway.db)
    event = _event("hello")

    adapter._active_sessions["telegram:c1:u1"] = True
    adapter.handle_message(event)

    health = adapter.health_snapshot()

    assert health["active_session_keys"] == ["telegram:c1:u1"]
    assert health["pending_messages"] == 1
    assert health["pending_by_session"] == {"telegram:c1:u1": 1}
    assert health["oldest_pending_age_seconds"] >= 0
    assert health["last_message_at"]


@pytest.mark.asyncio
async def test_base_adapter_handler_failure_drains_pending_queue(gateway):
    adapter = RecordingAdapter(gateway.config, gateway.db)
    calls = []

    async def handler(event):
        calls.append(event.text)
        if event.text == "first":
            raise RuntimeError("handler boom")
        return f"ok:{event.text}"

    adapter.set_message_handler(handler)
    adapter.handle_message(_event("first", message_id="m1"))
    adapter.handle_message(_event("second", message_id="m2"))

    await _wait_until(lambda: adapter.sent_contents == ["ok:second"])

    health = adapter.health_snapshot()
    assert calls == ["first", "second"]
    assert health["active_sessions"] == 0
    assert health["pending_messages"] == 0


@pytest.mark.asyncio
async def test_base_adapter_typing_failure_does_not_block_message(gateway):
    adapter = TypingFailAdapter(gateway.config, gateway.db)

    async def handler(event):
        return f"ok:{event.text}"

    adapter.set_message_handler(handler)
    adapter.handle_message(_event("hello", message_id="m1"))

    await _wait_until(lambda: adapter.sent_contents == ["ok:hello"])
    assert adapter.health_snapshot()["active_sessions"] == 0


@pytest.mark.asyncio
async def test_base_adapter_send_failure_drains_pending_queue(gateway):
    adapter = AlwaysFailSendAdapter(gateway.config, gateway.db)
    handled = []

    async def handler(event):
        handled.append(event.text)
        return f"reply:{event.text}"

    adapter.set_message_handler(handler)
    adapter.handle_message(_event("first", message_id="m1"))
    adapter.handle_message(_event("second", message_id="m2"))

    await _wait_until(lambda: handled == ["first", "second"] and adapter.send_attempts == 6)

    health = adapter.health_snapshot()
    assert health["active_sessions"] == 0
    assert health["pending_messages"] == 0
    assert health["last_send_error"] == "send failed"
    assert health["send_stats"]["failed_count"] == 2
    assert health["send_stats"]["retry_count"] == 4


@pytest.mark.asyncio
async def test_base_adapter_format_send_error_strips_formatting_and_retries(gateway):
    adapter = SequenceSendAdapter(gateway.config, gateway.db, [
        SendResult(success=False, error="markdown parse error"),
        SendResult(success=True, message_id="ok"),
    ])

    await adapter._send_with_retry("chat", "**hello** `code` [link](https://example.test)")

    assert adapter.sent_contents == [
        "**hello** `code` [link](https://example.test)",
        "hello code link",
    ]
    assert adapter.sleep_delays == [0.5]
    health = adapter.health_snapshot()
    assert health["send_stats"]["sent_count"] == 1
    assert health["send_stats"]["retry_count"] == 1
    assert health["send_stats"]["failed_count"] == 0


@pytest.mark.asyncio
async def test_base_adapter_timeout_send_error_does_not_retry(gateway):
    adapter = SequenceSendAdapter(gateway.config, gateway.db, [
        SendResult(success=False, error="request timeout"),
    ])

    await adapter._send_with_retry("chat", "hello")

    assert adapter.sent_contents == ["hello"]
    health = adapter.health_snapshot()
    assert health["send_stats"]["retry_count"] == 0
    assert health["send_stats"]["failed_count"] == 1
    assert health["last_send_error"] == "request timeout"


@pytest.mark.asyncio
async def test_base_adapter_send_exception_retries_to_limit(gateway):
    adapter = SequenceSendAdapter(gateway.config, gateway.db, [
        RuntimeError("temporary"),
        RuntimeError("temporary"),
        RuntimeError("temporary"),
    ])

    await adapter._send_with_retry("chat", "hello", max_retries=2)

    assert adapter.sent_contents == ["hello", "hello", "hello"]
    assert len(adapter.sleep_delays) == 2
    health = adapter.health_snapshot()
    assert health["send_stats"]["retry_count"] == 2
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

    await _wait_until(lambda: adapter.sent_contents == ["ok:same"])

    health = adapter.health_snapshot()
    assert handled == ["same"]
    assert health["dedupe_size"] == 1
    assert health["active_sessions"] == 0
    assert health["pending_messages"] == 0


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

    await _wait_until(lambda: adapter.sent_contents == ["ok:first", "ok:second"])

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

    await _wait_until(lambda: len(adapter.sent_contents) == 4)

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

    await _wait_until(lambda: len(adapter.sent_contents) == 4)

    assert handled == ["m1", "m2", "m3", "m1"]
    health = adapter.health_snapshot()
    assert health["dedupe_size"] == 2
    assert health["dedupe_max_size"] == 2


def test_base_adapter_uses_gateway_settings(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        """
gateway:
  platform_reconnect_delays: [3, 7, 11]
  platform_pending_warning_threshold: 4
  platform_chat_locks_maxsize: 5
  platform_message_dedupe_max_size: 6
  platform_send_max_retries: 0
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

    assert adapter._chat_locks_maxsize == 5
    assert adapter._pending_warning_threshold == 4
    assert adapter._dedupe_max_size == 6
    assert adapter._send_max_retries == 0

    runtime = PlatformRuntime(name="demo", backoff_delays_seconds=tuple(settings.platform_reconnect_delays))
    runtime.attempts = 1
    assert runtime.next_retry_delay() == 3
    runtime.attempts = 3
    assert runtime.next_retry_delay() == 11


@pytest.mark.asyncio
async def test_base_adapter_same_session_messages_are_serialized(gateway):
    adapter = RecordingAdapter(gateway.config, gateway.db)
    handled = []

    async def handler(event):
        handled.append(f"start:{event.text}")
        await asyncio.sleep(0.02)
        handled.append(f"end:{event.text}")
        return f"reply:{event.text}"

    adapter.set_message_handler(handler)
    adapter.handle_message(_event("one", message_id="m1"))
    adapter.handle_message(_event("two", message_id="m2"))
    adapter.handle_message(_event("three", message_id="m3"))

    await _wait_until(lambda: adapter.sent_contents == ["reply:one", "reply:two", "reply:three"])

    assert handled == [
        "start:one", "end:one",
        "start:two", "end:two",
        "start:three", "end:three",
    ]
    health = adapter.health_snapshot()
    assert health["active_sessions"] == 0
    assert health["pending_messages"] == 0


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
        return f"reply:{event.source.chat_id}"

    adapter.set_message_handler(handler)
    adapter.handle_message(_event("one", chat_id="c1", message_id="m1"))
    adapter.handle_message(_event("two", chat_id="c2", message_id="m2"))

    await asyncio.wait_for(both_started.wait(), timeout=1)
    assert sorted(started) == ["c1", "c2"]
    assert adapter.health_snapshot()["active_sessions"] == 2

    release.set()
    await _wait_until(lambda: sorted(adapter.sent_contents) == ["reply:c1", "reply:c2"])
    assert adapter.health_snapshot()["active_sessions"] == 0


@pytest.mark.asyncio
async def test_gateway_stop_marks_active_run(gateway):
    gateway._run_state.begin("telegram:c1:u1", _event("hello").source)

    stopped = await gateway._handle_command(_event("/stop"), "telegram:c1:u1")
    health = gateway.health_snapshot()

    assert stopped == "已停止。"
    assert health["stop_requested_agents"] == 1
    assert health["running_agent_runs"][0]["status"] == "stopping"


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
async def test_gateway_allow_and_stop_apply_to_cached_agents(gateway):
    gateway._agent_cache["telegram:c1:u1"] = Agent()
    gateway._agent_cache["telegram:work:u1"] = Agent()

    allowed = await gateway._handle_command(_event("/allow write"), "telegram:c1:u1")
    stopped = await gateway._handle_command(_event("/stop"), "telegram:c1:u1")

    assert "已授权 write" in allowed
    assert stopped == "已停止。"
    assert all("write" in agent._destructive_allowed for agent in gateway._agent_cache.values())
    assert all(agent._interrupt_requested for agent in gateway._agent_cache.values())


@pytest.mark.asyncio
async def test_gateway_stop_reports_delegate_agent_count(gateway, monkeypatch):
    gateway._agent_cache["telegram:c1:u1"] = Agent()
    monkeypatch.setattr(
        "personal_agent.plugins.builtin.tools.builtin.delegate.stop_delegate_agents",
        lambda: 3,
    )

    stopped = await gateway._handle_command(_event("/stop"), "telegram:c1:u1")

    assert stopped == "已停止。已请求停止 3 个子 agent。"
    assert all(agent._interrupt_requested for agent in gateway._agent_cache.values())
