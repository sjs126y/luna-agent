"""Context budget accounting."""

from personal_agent.context_budget import estimate_context_budget


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
