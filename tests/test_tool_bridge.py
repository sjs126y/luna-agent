import pytest


@pytest.mark.asyncio
async def test_tool_call_accepts_flattened_discovered_arguments():
    from luna_agent.plugins.builtin.tools.bridge.bridge import _tool_call

    result = await _tool_call("missing_discovered_tool", limit=20)

    assert result == "Error: unknown tool 'missing_discovered_tool'"


def test_tool_call_prefers_explicit_nested_arguments():
    from luna_agent.plugins.builtin.tools.bridge.bridge import _normalize_tool_arguments

    assert _normalize_tool_arguments(
        {"limit": 2},
        {"limit": 20, "trace_id": "trace-1"},
    ) == {"limit": 2, "trace_id": "trace-1"}
