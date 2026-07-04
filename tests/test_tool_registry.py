"""Test tool registry registration, definitions, dispatch."""

import json

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
    assert entry.tags == []
    assert entry.risk_level == "low"
    assert entry.usage_hint == ""


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
        tags=["file", "write"],
        risk_level="high",
        usage_hint="Use carefully.",
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
    assert catalog["writer"]["tags"] == ["file", "write"]
    assert catalog["writer"]["risk_level"] == "high"
    assert catalog["writer"]["usage_hint"] == "Use carefully."

    summary = registry.catalog_summary()
    assert summary["total"] == 2
    assert summary["available"] == 1
    assert summary["unavailable"] == 1
    assert summary["by_permission"] == {"read": 1, "write": 1}
    assert summary["by_toolset"] == {"builtin": 1, "custom": 1}
    assert summary["by_risk"] == {"high": 1, "low": 1}
    assert summary["by_tag"] == {"file": 1, "write": 1}
    assert summary["high_risk"] == ["writer"]
    assert summary["unavailable_tools"] == [{"name": "writer", "reason": "check_fn returned False"}]


@pytest.mark.asyncio
async def test_bridge_search_and_describe_include_tool_metadata():
    from personal_agent.tools.registry import (
        dispatch_tool_describe,
        dispatch_tool_search,
        tool_registry,
    )

    async def dummy(**kw):
        return "ok"

    original = tool_registry.get("metadata_bridge_demo")
    tool_registry.register(ToolEntry(
        name="metadata_bridge_demo",
        description="specialmeta bridge metadata demo",
        schema={"type": "object", "properties": {"value": {"type": "string"}}},
        handler=dummy,
        toolset="custom",
        permission_category="write",
        tags=["demo", "write"],
        risk_level="high",
        usage_hint="Use for metadata bridge tests.",
        is_destructive=True,
    ))
    try:
        search = json.loads(await dispatch_tool_search("specialmeta"))
        hit = search["hits"][0]
        assert hit["name"] == "metadata_bridge_demo"
        assert hit["permission_category"] == "write"
        assert hit["risk_level"] == "high"
        assert hit["tags"] == ["demo", "write"]
        assert hit["usage_hint"] == "Use for metadata bridge tests."

        described = json.loads(await dispatch_tool_describe("metadata_bridge_demo"))
        assert described["input_schema"]["properties"]["value"]["type"] == "string"
        assert described["toolset"] == "custom"
        assert described["available"] is True
        assert described["risk_level"] == "high"
    finally:
        if original is None:
            tool_registry.unregister("metadata_bridge_demo")
        else:
            tool_registry.register(original)
