"""Controlled multi-agent runtime."""

import pytest

from personal_agent.agents.runtime import AgentRuntime, AgentSpec
from personal_agent.models.messages import NormalizedResponse


@pytest.mark.asyncio
async def test_agent_runtime_defaults_to_readonly_tools():
    seen_tool_names = []

    async def call_fn(messages, system_prompt, tools, max_tokens):
        seen_tool_names.append([tool["name"] for tool in tools])
        return NormalizedResponse(
            text="done",
            usage={"input_tokens": 3, "output_tokens": 2},
        )

    runtime = AgentRuntime(
        call_fn=call_fn,
        tools=[
            {"name": "read", "description": "read", "input_schema": {}},
            {"name": "write", "description": "write", "input_schema": {}},
            {"name": "delegate_task", "description": "delegate", "input_schema": {}},
        ],
        max_tokens=100,
    )

    run = await runtime.run("inspect safely", AgentSpec(role="reviewer"))

    assert run.status == "completed"
    assert run.result == "done"
    assert run.usage == {"input_tokens": 3, "output_tokens": 2}
    assert seen_tool_names == [["read"]]


@pytest.mark.asyncio
async def test_agent_runtime_blocks_destructive_even_with_all_policy():
    seen_tool_names = []

    async def call_fn(messages, system_prompt, tools, max_tokens):
        seen_tool_names.append([tool["name"] for tool in tools])
        return NormalizedResponse(text="done")

    runtime = AgentRuntime(
        call_fn=call_fn,
        tools=[
            {"name": "read", "description": "read", "input_schema": {}},
            {"name": "bash", "description": "bash", "input_schema": {}},
        ],
        max_tokens=100,
    )

    run = await runtime.run("inspect safely", AgentSpec(role="reviewer", tool_policy="all"))

    assert run.status == "completed"
    assert seen_tool_names == [["read"]]


@pytest.mark.asyncio
async def test_agent_runtime_allows_destructive_when_explicitly_authorized():
    seen_tool_names = []

    async def call_fn(messages, system_prompt, tools, max_tokens):
        seen_tool_names.append([tool["name"] for tool in tools])
        return NormalizedResponse(text="done")

    runtime = AgentRuntime(
        call_fn=call_fn,
        tools=[
            {"name": "read", "description": "read", "input_schema": {}},
            {"name": "bash", "description": "bash", "input_schema": {}},
        ],
        max_tokens=100,
    )

    run = await runtime.run(
        "inspect with shell",
        AgentSpec(role="operator", tool_policy=["read", "bash"]),
        allow_destructive=True,
    )

    assert run.status == "completed"
    assert seen_tool_names == [["read", "bash"]]


@pytest.mark.asyncio
async def test_agent_runtime_retries_schema_output():
    calls = 0

    async def call_fn(messages, system_prompt, tools, max_tokens):
        nonlocal calls
        calls += 1
        if calls == 1:
            return NormalizedResponse(text="not json", usage={"input_tokens": 1, "output_tokens": 1})
        return NormalizedResponse(text='{"answer": "ok"}', usage={"input_tokens": 2, "output_tokens": 3})

    runtime = AgentRuntime(call_fn=call_fn, tools=[], max_tokens=100)
    run = await runtime.run(
        "return json",
        AgentSpec(
            role="formatter",
            tool_policy="none",
            output_schema={"type": "object", "required": ["answer"]},
        ),
    )

    assert run.status == "completed"
    assert '"answer": "ok"' in run.result
    assert calls == 2
    assert run.usage == {"input_tokens": 3, "output_tokens": 4}
