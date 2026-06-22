"""Test agent engine with mocked transport."""

import pytest

from personal_agent.agent.agent import init_agent, Agent
from personal_agent.agent.context import build_turn_context
from personal_agent.agent.loop import run_conversation
from personal_agent.agent.retry import RetryState
from personal_agent.models.messages import NormalizedResponse
from personal_agent.llm.provider import ProviderProfile


class MockTransport:
    """Fake transport that returns pre-configured responses."""

    def __init__(self, responses: list[NormalizedResponse]):
        self.responses = responses
        self.calls = 0

    async def call(self, messages, system_prompt="", tools=None, max_tokens=4096, stream=False):
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


@pytest.fixture
def provider():
    return ProviderProfile(name="test", base_url="http://test", api_key="k", model="m")


@pytest.mark.asyncio
async def test_simple_response(provider):
    """Agent returns final response when no tool_calls."""
    transport = MockTransport([
        NormalizedResponse(text="Hello!", finish_reason="end_turn",
                          usage={"input_tokens": 5, "output_tokens": 3}),
    ])
    agent = init_agent(transport, provider)
    ctx = build_turn_context(agent, "Hi")
    result = await run_conversation(agent, ctx)

    assert result["completed"]
    assert result["api_calls"] == 1
    assert result["messages"][-1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_empty_response_retry(provider):
    """Empty response triggers retry nudge."""
    transport = MockTransport([
        NormalizedResponse(text="", finish_reason="end_turn"),  # empty → retry
        NormalizedResponse(text="OK!", finish_reason="end_turn",
                          usage={"input_tokens": 5, "output_tokens": 2}),
    ])
    agent = init_agent(transport, provider)
    ctx = build_turn_context(agent, "Hi")
    result = await run_conversation(agent, ctx)

    assert result["completed"]
    assert transport.calls == 2
    assert "OK" in result["final_response"]


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

    ctx = build_turn_context(agent, "Test")
    result = await run_conversation(agent, ctx)

    assert result["completed"]
    assert transport.calls == 2
    assert "Done" in result["final_response"]

    # Verify tool_result was appended
    tool_results = [m for m in result["messages"] if isinstance(m.get("content"), list)
                    and any(b.get("type") == "tool_result" for b in m["content"])]
    assert len(tool_results) >= 1


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
