import pytest


@pytest.mark.asyncio
async def test_tool_call_accepts_flattened_discovered_arguments():
    from luna_agent.plugins.builtin.tools.bridge.bridge import _tool_call

    result = await _tool_call("missing_discovered_tool", limit=20)

    assert result == "Error: unknown tool 'missing_discovered_tool'"
