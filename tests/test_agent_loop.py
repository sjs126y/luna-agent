"""Test agent engine with mocked transport."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_agent.agent.agent import init_agent, Agent
from personal_agent.agent.context import build_turn_context
from personal_agent.agent.loop import run_conversation
from personal_agent.agent.retry import RetryState
from personal_agent.models.messages import NormalizedResponse
from personal_agent.llm.provider import ProviderProfile
from personal_agent.conversation.events import ConversationEventSink, EventRecorder
from personal_agent.conversation.steer import SteerManager


class MockTransport:
    """Fake transport that returns pre-configured responses."""

    def __init__(self, responses: list[NormalizedResponse]):
        self.responses = responses
        self.calls = 0
        self.call_messages = []
        self.call_tools = []

    async def call(self, messages, system_prompt="", tools=None, max_tokens=4096, stream=False):
        self.call_messages.append(messages)
        self.call_tools.append(tools)
        if self.calls >= len(self.responses):
            return NormalizedResponse(text="done", finish_reason="end_turn")
        resp = self.responses[self.calls]
        self.calls += 1
        return resp

    def build_request(self, messages, system_prompt, tools, max_tokens):
        return {}

    def convert_tool_definitions(self, tools):
        return tools

    def convert_messages(self, messages):
        return messages


class FailingTransport(MockTransport):
    async def call(self, messages, system_prompt="", tools=None, max_tokens=4096, stream=False):
        raise RuntimeError("transport boom")


class PlanProbeTransport(MockTransport):
    def __init__(self, responses: list[NormalizedResponse]):
        super().__init__(responses)
        self.request_plan = None

    def build_request_from_plan(self, plan, max_tokens):
        return {}

    def last_cache_diagnostics(self):
        return {}

    async def call(
        self,
        messages,
        system_prompt="",
        tools=None,
        max_tokens=4096,
        stream=False,
        request_plan=None,
    ):
        self.request_plan = request_plan
        return await super().call(messages, system_prompt, tools, max_tokens, stream)


class SteerAddingTransport(MockTransport):
    def __init__(
        self,
        responses: list[NormalizedResponse],
        *,
        manager: SteerManager,
        session_key: str,
    ):
        super().__init__(responses)
        self.manager = manager
        self.session_key = session_key

    async def call(self, messages, system_prompt="", tools=None, max_tokens=4096, stream=False):
        if self.calls == 0:
            self.manager.add(self.session_key, None, "请按新的补充重新回答")
        return await super().call(messages, system_prompt, tools, max_tokens, stream)


class ProbeCompressor:
    threshold_tokens = 1
    protect_head = 2
    protect_tail = 6
    last_prompt_tokens = 0

    def __init__(self, *, should=False):
        self.should = should
        self.seen_token_count = 0
        self.updated_usage = None

    def should_compress(self, token_count, messages):
        self.seen_token_count = token_count
        return self.should

    async def compress(self, messages, system_prompt, transport):
        return messages

    def update_from_response(self, response):
        self.updated_usage = response.usage
        self.last_prompt_tokens = response.usage.get("input_tokens", 0)


@pytest.fixture
def provider():
    return ProviderProfile(name="test", base_url="http://test", api_key="k", model="m")


@pytest.mark.asyncio
async def test_simple_response(provider):
    """Agent returns final response when no tool_calls."""
    from personal_agent.conversation.events import EventRecorder

    recorder = EventRecorder()
    transport = MockTransport([
        NormalizedResponse(text="Hello!", finish_reason="end_turn",
                          usage={"input_tokens": 5, "output_tokens": 3}),
    ])
    agent = init_agent(transport, provider)
    ctx = await build_turn_context(agent,"Hi")
    result = await run_conversation(agent, ctx, event_sink=recorder)

    assert result["completed"]
    assert result["api_calls"] == 1
    assert result["messages"][-1]["role"] == "assistant"
    report = result["turn_report"]
    assert report["status"] == "completed"
    assert report["completed"] is True
    assert report["llm"]["calls"] == 1
    assert report["llm"]["input_tokens"] == 5
    assert report["llm"]["output_tokens"] == 3
    assert report["llm"]["cache_hit_tokens"] == 0
    assert report["llm"]["cache_hit_rate"] == 0.0
    assert report["llm"]["cache_diagnostics"]["usage_reported"] is False
    assert report["llm"]["cache_diagnostics"]["usage_interpretation"] == (
        "provider_did_not_report_cache_usage"
    )
    assert report["tools"]["total"] == 0
    assert report["tool_truth"]["calls_total"] == 0
    assert report["tool_truth"]["results_total"] == 0
    assert report["tool_truth"]["assistant_claim"]["claimed_tool_use"] is False
    assert report["tool_truth"]["assistant_claim"]["claimed_but_no_tool_call"] is False
    assert report["tool_truth"]["warnings"] == []
    assert report["final_response_summary"] == "Hello!"
    assert report["llm"]["context_used_tokens"] > 0
    assert report["llm"]["context_budget"]["used"] == report["llm"]["context_used_tokens"]
    assert [event.type for event in recorder.events] == [
        "turn_start",
        "llm_start",
        "llm_end",
        "assistant_message",
        "turn_end",
    ]
    assert recorder.events[2].data["input_tokens"] == 5
    assert recorder.events[2].data["cache_hit_tokens"] == 0
    assert recorder.events[2].data["context_used_tokens"] > 0
    assert recorder.events[2].data["context_remaining_tokens"] >= 0
    assert recorder.events[2].data["context_budget"]["used"] == recorder.events[2].data["context_used_tokens"]
    assert recorder.events[3].message == "Hello!"


@pytest.mark.asyncio
async def test_run_conversation_consumes_pending_steer_before_llm_call(provider):
    recorder = EventRecorder()
    transport = MockTransport([
        NormalizedResponse(text="收到", finish_reason="end_turn"),
    ])
    agent = init_agent(transport, provider)
    ctx = await build_turn_context(agent, "Hi", turn_id="turn-1")
    steer = SteerManager()
    steer.begin_turn("cli:default:local", "turn-1")
    signal = steer.add("cli:default:local", None, "回答更短")

    result = await run_conversation(
        agent,
        ctx,
        event_sink=recorder,
        steer=steer,
        session_key="cli:default:local",
    )

    assert result["completed"] is True
    assert signal.status == "consumed"
    first_call_text = _user_text(transport.call_messages[0])
    assert "[高优先级运行中用户指令]" in first_call_text
    assert "优先级高于本轮较早的用户请求" in first_call_text
    assert "回答更短" in first_call_text
    assert [event.type for event in recorder.events][:3] == [
        "turn_start",
        "steer_consumed",
        "llm_start",
    ]
    assert result["turn_report"]["steer"]["consumed"] == 1
    assert result["turn_report"]["steer"]["consumed_ids"] == [signal.id]


@pytest.mark.asyncio
async def test_run_conversation_applies_steer_received_during_final_response(provider):
    steer = SteerManager()
    steer.begin_turn("cli:default:local", "turn-1")
    transport = SteerAddingTransport(
        [
            NormalizedResponse(text="旧答案", finish_reason="end_turn"),
            NormalizedResponse(text="新答案", finish_reason="end_turn"),
        ],
        manager=steer,
        session_key="cli:default:local",
    )
    agent = init_agent(transport, provider)
    ctx = await build_turn_context(agent, "Hi", turn_id="turn-1")

    result = await run_conversation(
        agent,
        ctx,
        steer=steer,
        session_key="cli:default:local",
    )

    assert result["final_response"] == "新答案"
    assert transport.calls == 2
    second_call_text = _user_text(transport.call_messages[1])
    assert "请按新的补充重新回答" in second_call_text
    assert [message["role"] for message in result["messages"][-3:]] == ["assistant", "user", "assistant"]


def _user_text(messages: list[dict]) -> str:
    return "\n".join(
        str(block.get("text") or "")
        for message in messages
        if message.get("role") == "user"
        for block in (message.get("content") or [])
        if isinstance(block, dict) and block.get("type") == "text"
    )


@pytest.mark.asyncio
async def test_run_conversation_passes_request_plan_to_supported_transport(provider):
    transport = PlanProbeTransport([
        NormalizedResponse(text="Hello!", finish_reason="end_turn",
                          usage={"input_tokens": 5, "output_tokens": 3}),
    ])
    agent = init_agent(transport, provider)
    ctx = await build_turn_context(agent, "Hi")

    await run_conversation(agent, ctx)

    assert transport.request_plan is not None
    assert transport.request_plan.stable_system == agent._cached_system_prompt
    assert transport.request_plan.current_user["role"] == "user"


@pytest.mark.asyncio
async def test_run_conversation_updates_compressor_usage(provider):
    compressor = ProbeCompressor()
    transport = MockTransport([
        NormalizedResponse(text="Hello!", finish_reason="end_turn",
                          usage={"input_tokens": 42, "output_tokens": 3}),
    ])
    agent = init_agent(transport, provider, compressor=compressor)
    ctx = await build_turn_context(agent, "Hi")

    await run_conversation(agent, ctx)

    assert compressor.last_prompt_tokens == 42
    assert compressor.updated_usage == {"input_tokens": 42, "output_tokens": 3}


@pytest.mark.asyncio
async def test_build_turn_context_counts_ephemeral_injections_once(provider, monkeypatch):
    class Memory:
        def __init__(self):
            self.calls = 0

        def get_system_prompt_text(self):
            return ""

        async def prefetch(self, user_message):
            self.calls += 1
            return [{
                "role": "user",
                "content": [{"type": "text", "text": f"[相关记忆] {user_message} " + "m" * 400}],
            }]

    memory = Memory()
    compressor = ProbeCompressor(should=False)
    transport = MockTransport([NormalizedResponse(text="ok", usage={"input_tokens": 1})])
    agent = init_agent(transport, provider, compressor=compressor, memory_manager=memory)
    agent._pending_skill_injection = "[技能注入] " + "i" * 400
    monkeypatch.setattr(
        "personal_agent.agent.context._load_skill_summaries",
        lambda: "[技能摘要] " + "s" * 400,
    )

    ctx = await build_turn_context(agent, "remember me")
    assert memory.calls == 1
    assert ctx.skill_summaries.startswith("[技能摘要]")
    assert ctx.skill_injection.startswith("[技能注入]")
    assert ctx.memory_injections_text.startswith("[相关记忆]")
    assert compressor.seen_token_count > 250
    assert agent._last_skill_summaries == ctx.skill_summaries
    assert agent._last_skill_injection == ctx.skill_injection
    assert agent._last_memory_injections == ctx.memory_injections_text

    from personal_agent.agent.loop import _build_api_messages

    api_messages = await _build_api_messages(agent, ctx)

    assert any("[相关记忆]" in block.get("text", "")
               for msg in api_messages for block in msg.get("content", []))
    assert memory.calls == 1


@pytest.mark.asyncio
async def test_build_turn_context_detects_same_length_compression(provider):
    class SameLengthCompressor(ProbeCompressor):
        def __init__(self):
            super().__init__(should=True)

        async def compress(self, messages, system_prompt, transport):
            result = [dict(message) for message in messages]
            result[0] = {
                "role": result[0]["role"],
                "content": [{"type": "text", "text": "compressed old message"}],
            }
            return result

    compressor = SameLengthCompressor()
    agent = init_agent(MockTransport([]), provider, compressor=compressor)

    ctx = await build_turn_context(
        agent,
        "current",
        history=[{"role": "user", "content": [{"type": "text", "text": "old message"}]}],
    )

    assert len(ctx.messages) == 2
    assert ctx.was_compressed is True
    assert ctx.current_turn_user_idx == 1


@pytest.mark.asyncio
async def test_empty_response_retry(provider):
    """Empty response triggers retry nudge."""
    from personal_agent.conversation.events import EventRecorder

    recorder = EventRecorder()
    transport = MockTransport([
        NormalizedResponse(text="", finish_reason="end_turn"),  # empty → retry
        NormalizedResponse(text="OK!", finish_reason="end_turn",
                          usage={"input_tokens": 5, "output_tokens": 2}),
    ])
    agent = init_agent(transport, provider)
    ctx = await build_turn_context(agent,"Hi")
    result = await run_conversation(agent, ctx, event_sink=recorder)

    assert result["completed"]
    assert transport.calls == 2
    assert "OK" in result["final_response"]
    assert result["turn_report"]["retries"][0]["category"] == "empty_response"
    assert result["turn_report"]["retries"][0]["max_attempts"] == agent._retry.MAX_EMPTY_CONTENT
    assert result["turn_report"]["retries"][0]["recoverable"] is True
    assert result["turn_report"]["llm"]["calls"] == 2
    retry_event = next(event for event in recorder.events if event.type == "retry")
    assert retry_event.data["category"] == "empty_response"
    assert retry_event.data["attempt"] == 1
    assert retry_event.data["max_attempts"] == agent._retry.MAX_EMPTY_CONTENT
    assert retry_event.data["recoverable"] is True
    assert "可发送给用户" in _user_text(transport.call_messages[-1])
    assert "可发送给用户" not in _user_text(result["messages"])


@pytest.mark.asyncio
async def test_empty_response_exhaustion_uses_safe_persisted_fallback(provider):
    transport = MockTransport([
        NormalizedResponse(text="", finish_reason="end_turn")
        for _ in range(3)
    ])
    agent = init_agent(transport, provider)
    ctx = await build_turn_context(agent, "Hi")

    result = await run_conversation(agent, ctx)

    assert transport.calls == 3
    assert result["final_response"] == "抱歉，模型没有返回可发送内容，请重试或让我用更短的格式回答。"
    assert result["messages"][-1]["role"] == "assistant"
    assert "empty response from model" not in result["final_response"]
    assert "可发送给用户" not in _user_text(result["messages"])


@pytest.mark.asyncio
async def test_legacy_empty_retry_messages_are_hidden_from_model_without_rewriting_history(provider):
    legacy_prompt = (
        "You just executed tools but gave no response. "
        "Please provide a summary of what was done and the results."
    )
    history = [
        {"role": "user", "content": [{"type": "text", "text": "请继续。"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "之前的正常回答"}]},
        {"role": "user", "content": [{"type": "text", "text": legacy_prompt}]},
        {"role": "user", "content": [{"type": "text", "text": "请继续。"}]},
        {"role": "user", "content": [{"type": "text", "text": "请继续。"}]},
    ]
    transport = MockTransport([NormalizedResponse(text="正常回复", finish_reason="end_turn")])
    agent = init_agent(transport, provider)
    ctx = await build_turn_context(agent, "新问题", history=history)

    result = await run_conversation(agent, ctx)

    request_text = _user_text(transport.call_messages[0])
    assert legacy_prompt not in request_text
    assert request_text.count("请继续。") == 1
    assert legacy_prompt in _user_text(result["messages"])


@pytest.mark.asyncio
async def test_tool_use_loop(provider):
    """Agent executes tool and continues."""
    transport = MockTransport([
        NormalizedResponse(
            text="", finish_reason="tool_use",
            tool_calls=[{"id": "c1", "name": "echo", "input": {"msg": "test"}}],
            usage={"input_tokens": 10, "output_tokens": 5},
        ),
        NormalizedResponse(text="Done!", finish_reason="end_turn",
                          usage={"input_tokens": 8, "output_tokens": 2}),
    ])
    agent = init_agent(transport, provider)

    # Register echo tool
    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.registry import tool_registry

    async def _echo(msg: str = ""):
        return f"Echo: {msg}"

    tool_registry.register(ToolEntry(
        name="echo", description="Echo", schema={
            "type": "object", "properties": {"msg": {"type": "string"}}
        }, handler=_echo,
    ))

    ctx = await build_turn_context(agent,"Test")
    result = await run_conversation(agent, ctx)

    assert result["completed"]
    assert transport.calls == 2
    assert "Done" in result["final_response"]
    report = result["turn_report"]
    assert report["llm"]["calls"] == 2
    assert report["tools"]["total"] == 1
    assert report["tools"]["success"] == 1
    assert report["tools"]["items"][0]["tool_name"] == "echo"
    assert report["tools"]["items"][0]["tool_use_id"] == "c1"
    assert report["tools"]["items"][0]["status"] == "success"
    assert report["tools"]["items"][0]["decision_stage"] == "runtime_guard"
    assert report["tool_truth"]["calls_total"] == 1
    assert report["tool_truth"]["results_total"] == 1
    assert report["tool_truth"]["llm_tool_call_count"] == 1
    assert report["tool_truth"]["tool_names"] == ["echo"]
    assert report["tool_truth"]["status_counts"]["success"] == 1
    assert report["tool_truth"]["assistant_claim"]["claimed_but_no_tool_call"] is False

    # Verify tool_result was appended
    tool_results = [m for m in result["messages"] if isinstance(m.get("content"), list)
                    and any(b.get("type") == "tool_result" for b in m["content"])]
    assert len(tool_results) >= 1

    second_request = transport.call_messages[1]
    user_index = next(
        index for index, message in enumerate(second_request)
        if message.get("role") == "user"
        and _user_text([message]) == "Test"
    )
    tool_use_index = next(
        index for index, message in enumerate(second_request)
        if any(block.get("type") == "tool_use" for block in message.get("content", []))
    )
    tool_result_index = next(
        index for index, message in enumerate(second_request)
        if any(block.get("type") == "tool_result" for block in message.get("content", []))
    )
    assert user_index < tool_use_index < tool_result_index


@pytest.mark.asyncio
async def test_post_tool_empty_uses_one_tool_free_finalization_without_persisting_prompt(provider):
    transport = MockTransport([
        NormalizedResponse(
            text="",
            finish_reason="tool_use",
            tool_calls=[{"id": "c1", "name": "post_tool_echo", "input": {"value": "memory"}}],
        ),
        NormalizedResponse(text="", finish_reason="end_turn"),
        NormalizedResponse(text="根据记忆工具结果，当前共有若干长期记忆。", finish_reason="end_turn"),
    ])
    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.registry import tool_registry

    async def _echo(value: str = ""):
        return '{"items": [{"id": "m1", "content": "likes tea"}]}'

    tool_registry.register(ToolEntry(
        name="post_tool_echo",
        description="Post-tool empty test",
        schema={"type": "object", "properties": {"value": {"type": "string"}}},
        handler=_echo,
    ))
    try:
        agent = init_agent(transport, provider)
        ctx = await build_turn_context(agent, "列出记忆")
        result = await run_conversation(agent, ctx)
    finally:
        tool_registry.unregister("post_tool_echo")

    assert transport.calls == 3
    assert transport.call_tools[-1] == []
    assert result["final_response"] == "根据记忆工具结果，当前共有若干长期记忆。"
    assert "The tools already completed" in _user_text(transport.call_messages[-1])
    assert "The tools already completed" not in _user_text(result["messages"])
    retries = result["turn_report"]["retries"]
    assert [item["category"] for item in retries] == ["post_tool_empty"]


@pytest.mark.asyncio
async def test_identical_successful_tool_call_finalizes_after_three_executions(provider):
    transport = MockTransport([
        NormalizedResponse(
            text="", finish_reason="tool_use",
            tool_calls=[{"id": "c1", "name": "echo_once", "input": {"msg": "test"}}],
        ),
        NormalizedResponse(
            text="", finish_reason="tool_use",
            tool_calls=[{"id": "c2", "name": "echo_once", "input": {"msg": "test"}}],
        ),
        NormalizedResponse(
            text="", finish_reason="tool_use",
            tool_calls=[{"id": "c3", "name": "echo_once", "input": {"msg": "test"}}],
        ),
        NormalizedResponse(
            text="", finish_reason="tool_use",
            tool_calls=[{"id": "c4", "name": "echo_once", "input": {"msg": "test"}}],
        ),
        NormalizedResponse(text="根据已有结果，测试内容已处理。", finish_reason="end_turn"),
    ])

    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.registry import tool_registry

    executions = 0

    async def _echo_once(msg: str = ""):
        nonlocal executions
        executions += 1
        return f"Echo: {msg}"

    tool_registry.register(ToolEntry(
        name="echo_once", description="Echo once", schema={
            "type": "object", "properties": {"msg": {"type": "string"}}
        }, handler=_echo_once,
    ))
    try:
        agent = init_agent(transport, provider)
        ctx = await build_turn_context(agent, "Test")
        result = await run_conversation(agent, ctx)
    finally:
        tool_registry.unregister("echo_once")

    assert transport.calls == 5
    assert executions == 3
    assert result["final_response"] == "根据已有结果，测试内容已处理。"
    assert "相同调用被重复请求" not in result["final_response"]
    assert transport.call_tools[-1] == []
    assert all(tools for tools in transport.call_tools[:-1])
    assert not any(
        "The identical call" in _user_text([message])
        for message in result["messages"]
    )
    assert "The identical call" in _user_text(transport.call_messages[-1])
    assert result["turn_report"]["tools"]["total"] == 3
    duplicate_retry = next(
        retry for retry in result["turn_report"]["retries"]
        if retry["category"] == "duplicate_tool_call"
    )
    assert duplicate_retry["attempt"] == 1
    assert duplicate_retry["max_attempts"] == 1


@pytest.mark.asyncio
async def test_duplicate_tool_finalization_empty_response_uses_safe_fallback(provider):
    repeated_call = {
        "name": "finalize_fallback_echo",
        "input": {"msg": "large result"},
    }
    transport = MockTransport([
        NormalizedResponse(
            text="",
            finish_reason="tool_use",
            tool_calls=[{"id": f"c{index}", **repeated_call}],
        )
        for index in range(1, 5)
    ] + [NormalizedResponse(text="", finish_reason="end_turn")])

    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.registry import tool_registry

    async def _echo(msg: str = ""):
        return "sensitive raw result that must not be exposed"

    tool_registry.register(ToolEntry(
        name="finalize_fallback_echo",
        description="Echo for finalization fallback",
        schema={"type": "object", "properties": {"msg": {"type": "string"}}},
        handler=_echo,
    ))
    try:
        agent = init_agent(transport, provider)
        ctx = await build_turn_context(agent, "Test")
        result = await run_conversation(agent, ctx)
    finally:
        tool_registry.unregister("finalize_fallback_echo")

    assert transport.calls == 5
    assert transport.call_tools[-1] == []
    assert result["final_response"] == (
        "工具已经执行，但模型未能根据已有结果生成最终回复。请换一种方式重试本次请求。"
    )
    assert "sensitive raw result" not in result["final_response"]


@pytest.mark.asyncio
async def test_stop_hook_can_continue_once_without_persisting_instruction(provider):
    from personal_agent.hooks import HookEvent, HookManager, StopOutcome

    transport = MockTransport([
        NormalizedResponse(text="draft", finish_reason="end_turn"),
        NormalizedResponse(text="revised", finish_reason="end_turn"),
    ])
    manager = HookManager()
    calls = 0

    async def require_revision(event):
        nonlocal calls
        calls += 1
        return StopOutcome(
            continue_turn=True,
            reason="needs revision",
            continuation_prompt="Answer with a shorter final response.",
        )

    manager.register(owner="test", event=HookEvent.STOP, callback=require_revision)
    agent = init_agent(transport, provider, hook_manager=manager)
    ctx = await build_turn_context(agent, "Test")

    result = await run_conversation(agent, ctx)

    assert transport.calls == 2
    assert calls == 1
    assert result["final_response"] == "revised"
    assert "Answer with a shorter final response." in _user_text(transport.call_messages[1])
    assert not any(
        "Answer with a shorter final response." in _user_text([message])
        for message in result["messages"]
    )


@pytest.mark.asyncio
async def test_tool_quota_denial_stops_agent_loop(provider):
    transport = MockTransport([
        NormalizedResponse(
            text="", finish_reason="tool_use",
            tool_calls=[{"id": "c1", "name": "quota_echo", "input": {"msg": "one"}}],
        ),
        NormalizedResponse(
            text="", finish_reason="tool_use",
            tool_calls=[{"id": "c2", "name": "quota_echo", "input": {"msg": "two"}}],
        ),
        NormalizedResponse(text="已根据现有结果完成总结。", finish_reason="end_turn"),
    ])

    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.registry import tool_registry

    async def _quota_echo(msg: str = ""):
        return f"Echo: {msg}"

    tool_registry.register(ToolEntry(
        name="quota_echo", description="Quota echo", schema={
            "type": "object", "properties": {"msg": {"type": "string"}}
        }, handler=_quota_echo,
    ))
    agent = init_agent(transport, provider, max_tool_calls_per_turn=1)
    ctx = await build_turn_context(agent, "Test")

    result = await run_conversation(agent, ctx)

    assert transport.calls == 3
    assert result["final_response"] == "已根据现有结果完成总结。"
    assert transport.call_tools[-1] == []
    assert "Do not call any more tools" in _user_text(transport.call_messages[-1])
    assert result["turn_report"]["tools"]["total"] == 2
    assert result["turn_report"]["tools"]["denied"] == 1


@pytest.mark.asyncio
async def test_tool_quota_finalization_uses_fallback_when_model_returns_empty(provider):
    transport = MockTransport([
        NormalizedResponse(
            text="", finish_reason="tool_use",
            tool_calls=[{"id": "c1", "name": "quota_fallback_echo", "input": {"msg": "one"}}],
        ),
        NormalizedResponse(
            text="", finish_reason="tool_use",
            tool_calls=[{"id": "c2", "name": "quota_fallback_echo", "input": {"msg": "two"}}],
        ),
        NormalizedResponse(text="", finish_reason="end_turn"),
    ])

    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.registry import tool_registry

    async def _quota_fallback_echo(msg: str = ""):
        return "done"

    tool_registry.register(ToolEntry(
        name="quota_fallback_echo", description="Quota fallback echo",
        schema={"type": "object", "properties": {"msg": {"type": "string"}}},
        handler=_quota_fallback_echo,
    ))
    agent = init_agent(transport, provider, max_tool_calls_per_turn=1)
    ctx = await build_turn_context(agent, "Test")

    result = await run_conversation(agent, ctx)

    assert transport.calls == 3
    assert result["final_response"] == "已达到本轮工具调用上限（1），已停止继续调用工具。"


@pytest.mark.asyncio
async def test_iteration_limit_returns_nonempty_stop_message(provider):
    transport = MockTransport([
        NormalizedResponse(
            text="", finish_reason="tool_use",
            tool_calls=[{"id": "c1", "name": "iteration_echo", "input": {"msg": "one"}}],
        ),
        NormalizedResponse(text="should not run", finish_reason="end_turn"),
    ])

    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.registry import tool_registry

    async def _iteration_echo(msg: str = ""):
        return f"Echo: {msg}"

    tool_registry.register(ToolEntry(
        name="iteration_echo", description="Iteration echo", schema={
            "type": "object", "properties": {"msg": {"type": "string"}}
        }, handler=_iteration_echo,
    ))
    agent = init_agent(transport, provider, max_iterations=1)
    ctx = await build_turn_context(agent, "Test")

    result = await run_conversation(agent, ctx)

    assert transport.calls == 1
    assert result["final_response"] == "已达到本轮处理迭代上限，已停止继续调用工具。"
    assert result["turn_report"]["retries"][-1]["category"] == "iteration_budget"
    assert result["turn_report"]["retries"][-1]["recoverable"] is False


@pytest.mark.asyncio
async def test_turn_report_records_denied_tool_decision(provider):
    transport = MockTransport([
        NormalizedResponse(
            text="", finish_reason="tool_use",
            tool_calls=[{"id": "w1", "name": "danger", "input": {"value": "x"}}],
            usage={"input_tokens": 4, "output_tokens": 1},
        ),
    ])
    agent = init_agent(transport, provider)

    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.registry import tool_registry

    async def _danger(value: str = ""):
        return f"danger:{value}"

    tool_registry.register(ToolEntry(
        name="danger",
        description="Danger",
        schema={"type": "object", "properties": {"value": {"type": "string"}}},
        handler=_danger,
        permission_category="write",
        is_destructive=True,
        approval_mode="cached",
    ))

    from personal_agent.security.session import SecurityStateStore
    agent._security_context = SecurityStateStore(SimpleNamespace(
        execution_mode="ask-first",
        sandbox_roots=[Path.cwd()],
        permission_grant_ttl_minutes=60,
        tool_approval_config={},
    )).context("denied-tool")
    agent._security_grant_ttl_seconds = 3600

    ctx = await build_turn_context(agent, "Test")
    result = await run_conversation(agent, ctx)

    item = result["turn_report"]["tools"]["items"][0]
    assert transport.calls == 1
    assert "支持授权确认的入口" in result["final_response"]
    assert result["turn_report"]["tools"]["denied"] == 1
    assert item["tool_name"] == "danger"
    assert item["tool_use_id"] == "w1"
    assert item["status"] == "denied"
    assert item["decision_stage"] == "permission"
    assert item["permission_category"] == "write"
    assert item["reason_code"] == "security_approval_required"
    assert item["required_allow"] == "security"
    truth = result["turn_report"]["tool_truth"]
    assert truth["calls_total"] == 1
    assert truth["results_total"] == 1
    assert truth["status_counts"]["denied"] == 1
    assert truth["assistant_claim"]["claimed_but_no_tool_call"] is False


@pytest.mark.asyncio
async def test_permission_required_network_tool_stops_without_looping(provider):
    transport = MockTransport([
        NormalizedResponse(
            text="我先搜索一下。",
            finish_reason="tool_use",
            tool_calls=[{"id": "s1", "name": "web_search", "input": {"query": "GPT-5.5"}}],
            usage={"input_tokens": 4, "output_tokens": 1},
        ),
        NormalizedResponse(
            text="This should not be called",
            finish_reason="end_turn",
            usage={"input_tokens": 3, "output_tokens": 2},
        ),
    ])

    from personal_agent.security.session import SecurityStateStore
    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.registry import tool_registry

    async def _search(query: str = "", max_results: int = 3):
        return f"searched:{query}:{max_results}"

    tool_registry.register(ToolEntry(
        name="web_search",
        description="Search",
        schema={"type": "object", "properties": {"query": {"type": "string"}}},
        handler=_search,
        permission_category="network",
    ))
    agent = init_agent(transport, provider)
    agent._security_context = SecurityStateStore(SimpleNamespace(
        execution_mode="ask-first",
        sandbox_roots=[Path.cwd()],
        permission_grant_ttl_minutes=60,
        tool_approval_config={},
    )).context("network-denied")
    agent._security_grant_ttl_seconds = 3600

    ctx = await build_turn_context(agent, "试一下搜索")
    result = await run_conversation(agent, ctx)

    assert result["completed"] is True
    assert transport.calls == 1
    assert "支持授权确认的入口" in result["final_response"]
    assert result["turn_report"]["llm"]["calls"] == 1
    assert result["turn_report"]["tools"]["total"] == 1
    assert result["turn_report"]["tools"]["denied"] == 1
    item = result["turn_report"]["tools"]["items"][0]
    assert item["tool_name"] == "web_search"
    assert item["reason_code"] == "security_approval_required"
    assert item["permission_category"] == "network"


@pytest.mark.asyncio
async def test_hard_safety_denial_forces_tool_free_finalization(provider):
    transport = MockTransport([
        NormalizedResponse(
            text="我先读取受保护文件。",
            finish_reason="tool_use",
            tool_calls=[{"id": "g1", "name": "guarded_search", "input": {"query": "secret"}}],
            usage={"input_tokens": 4, "output_tokens": 2},
        ),
        NormalizedResponse(
            text="受保护资源未被访问。",
            finish_reason="end_turn",
            usage={"input_tokens": 5, "output_tokens": 3},
        ),
    ])

    from personal_agent.tools.entry import ToolEntry, ToolHandlerOutput
    from personal_agent.tools.registry import tool_registry

    async def _guarded_search(query: str = ""):
        return ToolHandlerOutput(
            text="Error: path blocked by sandbox",
            is_error=True,
            metadata={"reason_code": "sandbox_blocked"},
        )

    tool_registry.register(ToolEntry(
        name="guarded_search",
        description="Guarded search",
        schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=_guarded_search,
        permission_category="read",
    ))
    agent = init_agent(transport, provider)
    ctx = await build_turn_context(agent, "读取受保护文件")

    result = await run_conversation(agent, ctx)

    assert result["completed"] is True
    assert result["final_response"] == "受保护资源未被访问。"
    assert transport.calls == 2
    assert transport.call_tools[1] == []
    item = result["turn_report"]["tools"]["items"][0]
    assert item["status"] == "denied"
    assert item["reason_code"] == "sandbox_blocked"
    assert any(
        event["category"] == "hard_safety_denial"
        for event in result["turn_report"]["retries"]
    )


@pytest.mark.asyncio
async def test_temporary_network_grant_survives_turn_reset(provider):
    transport = MockTransport([
        NormalizedResponse(
            text="我先搜索一下。",
            finish_reason="tool_use",
            tool_calls=[{"id": "s1", "name": "web_search", "input": {"query": "GPT-5.5"}}],
            usage={"input_tokens": 4, "output_tokens": 1},
        ),
        NormalizedResponse(
            text="搜索完成。",
            finish_reason="end_turn",
            usage={"input_tokens": 3, "output_tokens": 2},
        ),
    ])

    from personal_agent.security.models import ResourceRequirement
    from personal_agent.security.session import SecurityStateStore
    from personal_agent.tools.entry import ToolEntry
    from personal_agent.tools.registry import tool_registry

    async def _search(query: str = "", max_results: int = 3):
        return f"searched:{query}:{max_results}"

    tool_registry.register(ToolEntry(
        name="web_search",
        description="Search",
        schema={"type": "object", "properties": {"query": {"type": "string"}}},
        handler=_search,
        permission_category="network",
    ))
    agent = init_agent(transport, provider)
    store = SecurityStateStore(SimpleNamespace(
        execution_mode="local-auto",
        sandbox_roots=[Path.cwd()],
        permission_grant_ttl_minutes=60,
        tool_approval_config={},
    ))
    agent._security_context = store.context("network-granted")
    agent._security_grant_ttl_seconds = store.grant_ttl_seconds
    network = ResourceRequirement("network", "web-search", "connect", "web_search")
    agent._security_context.state.grant_resource(network, ttl_seconds=3600)

    ctx = await build_turn_context(agent, "试一下搜索")
    result = await run_conversation(agent, ctx)

    assert result["completed"] is True
    assert result["final_response"] == "搜索完成。"
    assert transport.calls == 2
    item = result["turn_report"]["tools"]["items"][0]
    assert item["status"] == "success"
    assert agent._security_context.state.has_resource_grant(network)
    assert item["temporary_grant_ttl_seconds"] == 60 * 60
    assert item["required_allow"] == ""


@pytest.mark.asyncio
async def test_turn_report_flags_claimed_tool_use_without_tool_call(provider):
    transport = MockTransport([
        NormalizedResponse(
            text="好的，我现在并行读取全部 md 文件。",
            finish_reason="end_turn",
            usage={"input_tokens": 4, "output_tokens": 6},
        ),
    ])
    agent = init_agent(transport, provider)
    ctx = await build_turn_context(agent, "读取所有 md 文件")

    result = await run_conversation(agent, ctx)

    truth = result["turn_report"]["tool_truth"]
    assert truth["calls_total"] == 0
    assert truth["llm_tool_call_count"] == 0
    assert truth["assistant_claim"]["claimed_tool_use"] is True
    assert truth["assistant_claim"]["claimed_but_no_tool_call"] is True
    assert truth["assistant_claim"]["claim_phrases"]
    assert truth["warnings"] == ["assistant_claimed_tool_use_without_tool_call"]


@pytest.mark.asyncio
async def test_turn_report_does_not_treat_tool_advice_as_completed_use(provider):
    transport = MockTransport([
        NormalizedResponse(
            text="我建议你读取 README，搜索结果需要进一步验证。",
            finish_reason="end_turn",
            usage={"input_tokens": 4, "output_tokens": 8},
        ),
    ])
    agent = init_agent(transport, provider)
    ctx = await build_turn_context(agent, "怎么检查项目？")

    result = await run_conversation(agent, ctx)

    claim = result["turn_report"]["tool_truth"]["assistant_claim"]
    assert claim["claimed_tool_use"] is False
    assert claim["claimed_but_no_tool_call"] is False


@pytest.mark.asyncio
async def test_llm_failure_returns_failed_status(provider):
    from personal_agent.conversation.events import EventRecorder

    recorder = EventRecorder()
    agent = init_agent(FailingTransport([]), provider)
    ctx = await build_turn_context(agent, "Hi")

    result = await run_conversation(agent, ctx, event_sink=recorder)

    assert result["completed"] is False
    assert result["status"] == "failed"
    assert result["error"] == "RuntimeError: transport boom"
    assert "模型调用出错" in result["final_response"]
    assert result["turn_report"]["status"] == "failed"
    assert result["turn_report"]["error"] == "RuntimeError: transport boom"
    error_event = next(event for event in recorder.events if event.type == "error")
    assert error_event.data["category"] == "llm"
    assert error_event.data["recoverable"] is False
    assert error_event.data["detail_id"].startswith("err_")


@pytest.mark.asyncio
async def test_interrupt_emits_structured_stop_event(provider):
    from personal_agent.conversation.events import EventRecorder

    recorder = EventRecorder()
    agent = init_agent(MockTransport([]), provider)
    ctx = await build_turn_context(agent, "Hi")
    agent._interrupt_requested = True

    result = await run_conversation(agent, ctx, event_sink=recorder)

    assert result["turn_report"]["status"] == "stopped"
    stop_event = next(event for event in recorder.events if event.type == "stop")
    assert stop_event.data == {
        "reason": "user",
        "message": "已停止",
        "stopped_tools": 0,
        "stopped_agents": 0,
    }


@pytest.mark.asyncio
async def test_retry_state_reset():
    """RetryState resets correctly."""
    rs = RetryState()
    rs.empty_content_retries = 2
    rs.invalid_tool_retries = 1
    rs.post_tool_empty_retried = True
    rs.reset()
    assert rs.empty_content_retries == 0
    assert rs.invalid_tool_retries == 0
    assert not rs.post_tool_empty_retried


# ── streaming delta events (Phase 2 platform-safe gate) ────────────────

class DeltaTransport(MockTransport):
    """Transport that fires on_delta like a real streaming backend, then
    returns the assembled response."""

    def __init__(self, text="Hi", thinking=""):
        super().__init__([
            NormalizedResponse(text=text, finish_reason="end_turn",
                               usage={"input_tokens": 1, "output_tokens": 1}),
        ])
        self._text = text
        self._thinking = thinking

    async def call(self, messages, system_prompt="", tools=None, max_tokens=4096,
                   stream=False, on_delta=None):
        if on_delta is not None:
            if self._thinking:
                await on_delta("thinking", self._thinking)
            for ch in self._text:
                await on_delta("text", ch)
        return await super().call(messages, system_prompt, tools, max_tokens, stream)


class DeltaSink(ConversationEventSink):
    wants_deltas = True

    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


class PlainSink(ConversationEventSink):
    """Platform-style sink: wants_deltas defaults False."""

    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


@pytest.mark.asyncio
async def test_streaming_emits_deltas_when_sink_opts_in(provider):
    transport = DeltaTransport(text="Hey", thinking="pondering")
    agent = init_agent(transport, provider)
    ctx = await build_turn_context(agent, "Hi")
    sink = DeltaSink()

    result = await run_conversation(agent, ctx, event_sink=sink)

    kinds = [e.type for e in sink.events]
    assert "thinking_delta" in kinds
    assert "assistant_delta" in kinds
    text_deltas = [e for e in sink.events if e.type == "assistant_delta"]
    assert len(text_deltas) == 3  # "Hey" → 3 chars
    assert "".join(e.data["chunk"] for e in text_deltas) == "Hey"
    assert result["completed"]
    assert result["turn_report"]["event_counts"]["assistant_delta"] == 3
    assert result["turn_report"]["event_counts"]["thinking_delta"] == 1


@pytest.mark.asyncio
async def test_no_deltas_when_sink_opts_out(provider):
    """Platform path: wants_deltas defaults False, so on_delta is never wired
    and no delta events are produced even though the transport supports them."""
    transport = DeltaTransport(text="Hey", thinking="pondering")
    agent = init_agent(transport, provider)
    ctx = await build_turn_context(agent, "Hi")
    sink = PlainSink()

    result = await run_conversation(agent, ctx, event_sink=sink)

    kinds = [e.type for e in sink.events]
    assert "assistant_delta" not in kinds
    assert "thinking_delta" not in kinds
    # Final answer still arrives via assistant_message
    assert "assistant_message" in kinds
    assert result["completed"]
