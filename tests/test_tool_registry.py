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


def test_catalog_reports_metadata_and_availability(registry):
    async def dummy(**kw):
        return "ok"

    registry.register(ToolEntry(
        name="read",
        description="Read a file",
        schema={"type": "object", "properties": {"path": {"type": "string"}}},
        handler=dummy,
        toolset="builtin",
        permission_category="read",
    ))
    registry.register(ToolEntry(
        name="writer",
        description="Write something",
        schema={},
        handler=dummy,
        toolset="custom",
        permission_category="write",
        check_fn=lambda: False,
        precheck=lambda args: None,
        is_parallel_safe=False,
        is_destructive=True,
    ))

    catalog = {item["name"]: item for item in registry.catalog()}

    assert catalog["read"]["available"] is True
    assert catalog["read"]["groups"] == ["file"]
    assert catalog["read"]["input_properties"] == ["path"]
    assert catalog["writer"]["available"] is False
    assert catalog["writer"]["unavailable_reason"] == "check_fn returned False"
    assert catalog["writer"]["has_precheck"] is True
    assert catalog["writer"]["is_destructive"] is True

    summary = registry.catalog_summary()
    assert summary["total"] == 2
    assert summary["available"] == 1
    assert summary["unavailable"] == 1
    assert summary["by_permission"] == {"read": 1, "write": 1}
    assert summary["by_toolset"] == {"builtin": 1, "custom": 1}
    assert summary["high_risk"] == ["writer"]
    assert summary["unavailable_tools"] == [{"name": "writer", "reason": "check_fn returned False"}]
