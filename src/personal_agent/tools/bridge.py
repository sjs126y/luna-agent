"""Bridge tools — registered on import as normal tools.
When LLM calls tool_search/describe/call, these handlers execute via the
deferrable tool catalog managed by ToolRegistry.
"""

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import (
    tool_registry,
    dispatch_tool_search,
    dispatch_tool_describe,
    dispatch_tool_call,
)


async def _tool_search(query: str) -> str:
    return await dispatch_tool_search(query)


async def _tool_describe(name: str) -> str:
    return await dispatch_tool_describe(name)


async def _tool_call(name: str, arguments: dict) -> str:
    return await dispatch_tool_call(name, arguments)


tool_registry.register(ToolEntry(
    name="tool_search",
    description="Search available tools by keyword. Returns matching tool names and descriptions.",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords to search for in tool names and descriptions"},
        },
        "required": ["query"],
    },
    handler=_tool_search,
    toolset="system",
    is_parallel_safe=True,
))

tool_registry.register(ToolEntry(
    name="tool_describe",
    description="Get the full parameter schema for a specific tool.",
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Exact tool name from tool_search results"},
        },
        "required": ["name"],
    },
    handler=_tool_describe,
    toolset="system",
    is_parallel_safe=True,
))

tool_registry.register(ToolEntry(
    name="tool_call",
    description="Execute a tool by name with given arguments.",
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Tool name to execute"},
            "arguments": {"type": "object", "description": "Tool arguments as a JSON object"},
        },
        "required": ["name", "arguments"],
    },
    handler=_tool_call,
    toolset="system",
    is_parallel_safe=True,
))
