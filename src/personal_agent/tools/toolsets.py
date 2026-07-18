"""Centralized toolset definitions + core tool list.

Hermes pattern: tools don't declare themselves as "core" — a central
list decides which tools get full schemas vs get deferred via bridge tools.
"""

# Tools that define the agent's everyday execution surface. Less common
# capabilities remain registered and are discovered through tool_search.
_CORE_TOOLS: set[str] = {
    "read", "write", "edit", "list_directory", "file_info", "grep", "glob", "bash",
    "web_search", "web_fetch",
    "memory", "memory_buffer",
    "skill_search", "skill_load",
    "sub_agent",
    "process_start", "process_read", "process_kill", "process_wait",
}

# Toolset groups — name → list of tool names
# "all" is special: includes every registered tool
TOOLSETS: dict[str, set[str]] = {
    "web":      {"web_search", "web_fetch"},
    "terminal": {"bash"},
    "file":     {"read", "write", "edit", "list_directory", "file_info", "grep", "glob"},
    "utility":  {"calculator", "datetime", "random", "timer", "json"},
    "memory":   {"memory", "memory_buffer", "todo"},
    "info":     {"weather"},
    "mcp":      set(),  # MCP tools are registered dynamically at startup
    "code":     {"execute_code", "delegate_task"},
    "interact": {
        "clarify", "confirm",
        "process_start", "process_list", "process_read", "process_clear", "process_kill", "process_wait",
    },
}


def resolve_toolsets(names: list[str] | None, all_tool_names: set[str]) -> set[str]:
    """Resolve toolset names → concrete tool name set.

    names=None or names=["all"] → all registered tools.
    names=["web","terminal"] → union of those groups.
    Unknown names are silently ignored.
    """
    if names is None or "all" in names:
        return all_tool_names

    result: set[str] = set()
    for name in names:
        if name in TOOLSETS:
            result.update(TOOLSETS[name])
    return result


def is_core_tool(name: str) -> bool:
    return name in _CORE_TOOLS


def get_core_tools() -> set[str]:
    return _CORE_TOOLS.copy()
