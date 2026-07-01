"""Context budget accounting."""

import pytest

from personal_agent.context_budget import build_context_budget, estimate_context_budget


def test_context_budget_splits_tools_skills_memory_and_mcp():
    messages = [{
        "role": "user",
        "content": [{"type": "text", "text": "hello world"}],
    }]
    tools = [
        {
            "name": "read",
            "description": "read files",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "mcp__demo__search",
            "description": "[MCP demo] search",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    ]

    budget = estimate_context_budget(
        messages=messages,
        system_prompt="system prompt",
        tools=tools,
        skills_summary="available skills",
        memory_injections="memory hit",
        context_limit=1000,
        compression_threshold_ratio=0.6,
    )

    assert budget.system_prompt > 0
    assert budget.history_messages > 0
    assert budget.tools_schema > 0
    assert budget.mcp_tools > 0
    assert budget.skills > 0
    assert budget.memory_injections > 0
    assert budget.used == sum([
        budget.system_prompt,
        budget.history_messages,
        budget.tools_schema,
        budget.skills,
        budget.memory_injections,
        budget.mcp_tools,
    ])
    assert budget.remaining_context == 1000 - budget.used
    assert budget.compression_threshold == 600


@pytest.mark.asyncio
async def test_build_context_budget_collects_agent_inputs():
    class Provider:
        model = "deepseek-chat"
        context_window = 1000

    class Memory:
        async def prefetch(self, user_message):
            return [{
                "role": "user",
                "content": [{"type": "text", "text": f"[相关记忆] {user_message}"}],
            }]

    class Agent:
        _provider = Provider()
        model = "deepseek-chat"
        _cached_system_prompt = "system"
        tools = [{
            "name": "read",
            "description": "read files",
            "input_schema": {"type": "object", "properties": {}},
        }]
        _memory_manager = Memory()

    class Settings:
        compression_threshold_ratio = 0.5

    budget = await build_context_budget(
        messages=[{
            "role": "user",
            "content": [{"type": "text", "text": "hello"}],
        }],
        agent=Agent(),
        settings=Settings(),
        skills_summary="skills",
        current_user_message="remember me",
    )

    assert budget.context_limit == 1000
    assert budget.compression_threshold == 500
    assert budget.system_prompt > 0
    assert budget.tools_schema > 0
    assert budget.skills > 0
    assert budget.memory_injections > 0
