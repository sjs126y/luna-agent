"""Test tool registry registration, definitions, dispatch."""

import pytest

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import ToolRegistry


@pytest.fixture
def registry():
    return ToolRegistry()


@pytest.mark.asyncio
async def test_register_and_get(registry):
    async def dummy(**kw):
        return "ok"

    entry = ToolEntry(name="test", description="A test tool", schema={}, handler=dummy)
    registry.register(entry)
    assert registry.get("test") is entry
    assert registry.generation == 1


@pytest.mark.asyncio
async def test_get_definitions(registry):
    async def dummy(**kw):
        return "ok"

    registry.register(ToolEntry(
        name="calc", description="Calculate",
        schema={"type": "object", "properties": {}}, handler=dummy,
    ))
    defs = registry.get_definitions()
    # calc is deferrable → replaced by bridge tools
    names = [d["name"] for d in defs]
    assert "calc" not in names       # deferrable tools hidden
    assert "tool_search" in names    # bridge: search
    assert "tool_describe" in names  # bridge: describe
    assert "tool_call" in names      # bridge: call


@pytest.mark.asyncio
async def test_dispatch(registry):
    async def echo(message: str = ""):
        return f"Echo: {message}"

    registry.register(ToolEntry(
        name="echo", description="Echo back",
        schema={"type": "object", "properties": {"message": {"type": "string"}}},
        handler=echo,
    ))
    result = await registry.dispatch("echo", {"message": "hello"})
    assert result == "Echo: hello"


@pytest.mark.asyncio
async def test_dispatch_unknown(registry):
    result = await registry.dispatch("nonexistent", {})
    assert "Error" in result


@pytest.mark.asyncio
async def test_dispatch_error(registry):
    async def fail(**kw):
        raise ValueError("boom")

    registry.register(ToolEntry(name="fail", description="", schema={}, handler=fail))
    result = await registry.dispatch("fail", {})
    assert "Error" in result
    assert "boom" in result


@pytest.mark.asyncio
async def test_unregister(registry):
    async def dummy(**kw):
        return "ok"

    entry = ToolEntry(name="x", description="", schema={}, handler=dummy)
    registry.register(entry)
    assert registry.generation == 1
    registry.unregister("x")
    assert registry.get("x") is None
    assert registry.generation == 2
