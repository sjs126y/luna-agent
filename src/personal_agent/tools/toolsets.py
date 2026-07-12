"""Centralized toolset definitions + core tool list.

Hermes pattern: tools don't declare themselves as "core" — a central
list decides which tools get full schemas vs get deferred via bridge tools.
"""

# Tools that always get full schemas (never deferred)
_CORE_TOOLS: set[str] = {
    "calculator", "datetime", "web_search", "web_fetch",
    "bash", "memory", "memory_buffer", "todo", "read", "write", "edit",
    "weather", "random", "timer", "json",
    "grep", "glob",
    "skill_search", "skill_load",
    "clarify", "execute_code",
    "sub_agent", "sub_parallel", "sub_pipeline",
    "delegate_task", "run_research", "run_review", "run_workflow",
    "process_start", "process_list", "process_read", "process_clear", "process_kill", "process_wait",
    "confirm", "task",
    "workflow_run", "workflow_list",
    "worktree_create", "worktree_merge", "worktree_cleanup", "worktree_list",
}

# Toolset groups — name → list of tool names
# "all" is special: includes every registered tool
TOOLSETS: dict[str, set[str]] = {
    "web":      {"web_search", "web_fetch"},
    "terminal": {"bash"},
    "file":     {"read", "write", "edit", "grep", "glob"},
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
