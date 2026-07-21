"""Bridge tools — registered on import as normal tools.
When LLM calls tool_search/describe/call, these handlers manage deferrable tools.

Flow: tool_search (discover) → tool_describe (get schema) → LLM calls tool directly.
tool_call is a fallback for tools that cannot be surfaced mid-turn and dispatches
through the same executor security pipeline.
"""

from luna_agent.tools.entry import ToolEntry
from luna_agent.tools.registry import (
    tool_registry,
    dispatch_tool_search,
    dispatch_tool_describe,
)


async def _tool_search(query: str) -> str:
    return await dispatch_tool_search(query)


async def _tool_describe(name: str) -> str:
    """Return full schema for a tool. After the LLM sees this, it should
    call the tool directly by name in the next iteration — NOT via tool_call.
    """
    return await dispatch_tool_describe(name)


async def _tool_call(name: str, arguments: dict | None = None, **flat_arguments) -> object:
    """Execute a deferrable tool through the shared executor pipeline."""
    from luna_agent.tools.registry import tool_registry as _tr
    from luna_agent.tools.executor import execute_tool_call_result, format_tool_result
    from luna_agent.tools.runtime_context import (
        current_tool_agent,
        current_tool_confirm,
        current_tool_event_sink,
    )

    # Some providers flatten discovered-tool arguments beside ``name`` even
    # though the bridge schema advertises an ``arguments`` object. Normalize
    # both shapes before entering the shared executor pipeline.
    normalized_arguments = dict(arguments or {})
    normalized_arguments.update(flat_arguments)
    entry = _tr.get(name)
    if entry is None:
        return f"Error: unknown tool '{name}'"
    if name == "tool_call":
        return "Error: tool_call cannot call itself"

    agent = current_tool_agent()
    confirm = current_tool_confirm()
    event_sink = current_tool_event_sink()
    result = await execute_tool_call_result(
        {
            "id": f"tool_call:{name}",
            "name": name,
            "input": normalized_arguments,
        },
        agent=agent,
        confirm=confirm,
        event_sink=event_sink,
    )
    if agent is None and confirm is None and event_sink is None:
        return format_tool_result(result)
    return result


def _nested_tool_timeout(arguments: dict) -> float | None:
    name = str(arguments.get("name") or "")
    entry = tool_registry.get(name)
    if entry is None:
        return None
    resolver = entry.timeout_resolver
    if callable(resolver):
        nested_arguments = arguments.get("arguments")
        return resolver(nested_arguments if isinstance(nested_arguments, dict) else {})
    return entry.timeout_seconds


tool_registry.register(ToolEntry(
    name="tool_search",
    description="Search for tools by keyword. Returns matching tools with name, description, and "
                "full input_schema. After searching, call the matched tool DIRECTLY by name — "
                "you already have the schema and can construct the call immediately.",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords to search for in tool names and descriptions"},
        },
        "required": ["query"],
    },
    handler=_tool_search,
    toolset="system",
    tags=["tooling", "discovery"],
    risk_level="low",
    usage_hint="Use to discover a non-core tool by keyword before calling it directly.",
    is_parallel_safe=True,
))

tool_registry.register(ToolEntry(
    name="tool_describe",
    description="Get the full parameter schema for a specific tool. "
                "After calling this, call the tool directly by name.",
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Exact tool name from tool_search results"},
        },
        "required": ["name"],
    },
    handler=_tool_describe,
    toolset="system",
    tags=["tooling", "discovery"],
    risk_level="low",
    usage_hint="Use to inspect one tool's schema, permissions, and risk metadata before calling it.",
    is_parallel_safe=True,
))

tool_registry.register(ToolEntry(
    name="tool_call",
    description="Execute a discovered tool by name through the shared security and audit pipeline.",
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Tool name to execute"},
            "arguments": {"type": "object", "description": "Tool arguments as a JSON object"},
        },
        "required": ["name", "arguments"],
        "additionalProperties": True,
    },
    handler=_tool_call,
    toolset="system",
    tags=["tooling", "dispatch"],
    risk_level="medium",
    usage_hint="Use for a discovered tool that is not directly visible; direct calls remain preferred.",
    is_parallel_safe=False,
    report_as_tool=False,
    timeout_resolver=_nested_tool_timeout,
))
