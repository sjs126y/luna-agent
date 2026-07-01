"""Sub-agent tools — CC-style multi-agent primitives.

sub_agent:     Spawn one sub-agent for a focused task (parallel-safe)
sub_parallel:  Run multiple sub-agents concurrently, wait for all
sub_pipeline:  Run items through stages independently (no barrier)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

from personal_agent.agents.runtime import AgentRuntime, AgentSpec, DESTRUCTIVE_TOOLS
from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry

logger = logging.getLogger(__name__)

_delegate_call: Callable | None = None
_agent_runtime: AgentRuntime = AgentRuntime()


def setup_delegate(call_fn, tools, max_tokens=4096):
    global _delegate_call, _agent_runtime
    _delegate_call = call_fn
    _agent_runtime = AgentRuntime(call_fn=call_fn, tools=tools, max_tokens=max_tokens)


# Default: read-only tools a sub-agent always gets
_READONLY_TOOLS = {
    "read", "grep", "glob", "web_search", "web_fetch",
    "calculator", "datetime", "weather", "random", "json",
    "todo", "task", "process_list",
}

# Tools the main agent can optionally grant
_GRANTABLE_TOOLS = {"write", "edit", "bash", "execute_code", "process_kill", "memory"}

async def _run_agent(prompt, system_prompt="", schema="", max_tokens=2048,
                     allowed_tools=None, allowed_categories=None):
    """Run a single-turn sub-agent.

    allowed_tools: explicit tool names to grant (e.g. ["write", "bash"]).
                   Default: read-only set.
    allowed_categories: shortcut — "all" grants everything grantable,
                        "readonly" (default) gives only safe tools.
    """
    if _delegate_call is None:
        return "Error: sub-agent system not initialized"

    output_schema = _parse_schema(schema)
    tool_policy = _tool_policy_from_legacy(allowed_tools, allowed_categories)
    allow_destructive = _allows_destructive(tool_policy)
    spec = AgentSpec(
        role="sub-agent",
        system_prompt=system_prompt or (
            "You are a focused sub-agent. Complete the task and return your result concisely."
        ),
        tool_policy=tool_policy,
        max_tokens=max_tokens,
        output_schema=output_schema,
    )
    run = await _agent_runtime.run(prompt, spec, allow_destructive=allow_destructive)
    return _format_agent_result(run)


def _tool_policy_from_legacy(allowed_tools=None, allowed_categories=None) -> str | list[str]:
    if allowed_categories == "all":
        return sorted(_READONLY_TOOLS | _GRANTABLE_TOOLS)
    allowed_tools = _normalize_allowed_tools(allowed_tools)
    if allowed_tools:
        return sorted(_READONLY_TOOLS | (set(allowed_tools) & _GRANTABLE_TOOLS))
    return "readonly"


def _allows_destructive(policy: str | list[str]) -> bool:
    if policy == "all":
        return True
    if isinstance(policy, list):
        return bool(set(policy) & DESTRUCTIVE_TOOLS)
    return False


def _parse_schema(schema: str) -> dict | None:
    if not schema:
        return None
    try:
        value = json.loads(schema)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _normalize_allowed_tools(allowed_tools) -> list[str]:
    if not allowed_tools:
        return []
    if isinstance(allowed_tools, str):
        try:
            value = json.loads(allowed_tools)
        except json.JSONDecodeError:
            return []
        return [str(item) for item in value] if isinstance(value, list) else []
    if isinstance(allowed_tools, list):
        return [str(item) for item in allowed_tools]
    return []


async def _sub_agent(prompt, system_prompt="", schema="", max_tokens=2048,
                     allowed_tools="", allowed_categories=""):
    return await _run_agent(prompt, system_prompt, schema, max_tokens,
                            allowed_tools=allowed_tools,
                            allowed_categories=allowed_categories or None)


async def _sub_parallel(tasks_json):
    try:
        tasks = json.loads(tasks_json)
    except json.JSONDecodeError as e:
        return f"Error: invalid tasks JSON: {e}"
    if not isinstance(tasks, list) or not tasks:
        return "Error: tasks must be a non-empty JSON array"

    async def _one(task):
        return await _run_agent(
            prompt=task.get("prompt", ""),
            system_prompt=task.get("system_prompt", ""),
            schema=task.get("schema", ""),
            max_tokens=task.get("max_tokens", 2048),
            allowed_tools=task.get("allowed_tools"),
            allowed_categories=task.get("allowed_categories"))

    results = await asyncio.gather(*[_one(t) for t in tasks], return_exceptions=True)
    lines = []
    for i, r in enumerate(results):
        label = tasks[i].get("prompt", f"Task {i}")[:60]
        rt = str(r) if not isinstance(r, BaseException) else f"Error: {r}"
        lines.append(f"## {label}\n{rt}")
    return "\n\n".join(lines)


async def _sub_pipeline(items_json, stage_prompt, stage_system_prompt=""):
    try:
        items = json.loads(items_json)
    except json.JSONDecodeError as e:
        return f"Error: invalid items JSON: {e}"
    if not isinstance(items, list) or not items:
        return "Error: items must be a non-empty JSON array"

    async def _process(item, index):
        item_str = json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else str(item)
        prompt = stage_prompt.replace("{item}", item_str)
        return await _run_agent(prompt=prompt, system_prompt=stage_system_prompt, max_tokens=2048)

    results = await asyncio.gather(*[_process(item, i) for i, item in enumerate(items)], return_exceptions=True)
    lines = []
    for i, r in enumerate(results):
        label = str(items[i])[:60]
        rt = str(r) if not isinstance(r, BaseException) else f"Error: {r}"
        lines.append(f"## [{i}] {label}\n{rt}")
    return "\n\n".join(lines)


async def _delegate_task(task: str, role: str = "assistant", system_prompt: str = "",
                         tool_policy: str = "readonly", max_tokens: int = 2048) -> str:
    spec = AgentSpec(
        role=role,
        system_prompt=system_prompt,
        tool_policy=tool_policy or "readonly",
        max_tokens=max_tokens,
    )
    run = await _agent_runtime.run(task, spec)
    return _format_agent_run(run)


async def _run_research(question: str, max_tokens: int = 2048) -> str:
    spec = AgentSpec(
        role="researcher",
        system_prompt=(
            "You are a focused research sub-agent. Gather relevant facts with read-only tools "
            "and return a concise sourced summary when tools provide sources."
        ),
        tool_policy=["web_search", "web_fetch", "read", "grep", "glob", "calculator", "datetime"],
        max_tokens=max_tokens,
    )
    run = await _agent_runtime.run(question, spec)
    return _format_agent_run(run)


async def _run_review(target: str, focus: str = "bugs, security, performance",
                      max_tokens: int = 2048) -> str:
    spec = AgentSpec(
        role="code reviewer",
        system_prompt=(
            "You are a skeptical code review sub-agent. Use read-only tools only. "
            "Prioritize concrete bugs, regressions, security issues, and missing tests."
        ),
        tool_policy=["read", "grep", "glob"],
        max_tokens=max_tokens,
    )
    run = await _agent_runtime.run(
        f"Review target: {target}\nFocus: {focus}",
        spec,
    )
    return _format_agent_run(run)


async def _run_workflow(name: str, args: str = "{}") -> str:
    from personal_agent.workflow.engine import run_workflow_tool

    return await run_workflow_tool(name, args)


def _format_agent_run(run) -> str:
    if run.status != "completed":
        return run.result
    usage = run.usage
    return (
        f"{run.result}\n\n"
        f"[agent_run id={run.run_id} status={run.status} "
        f"duration={run.duration:.2f}s input={usage.get('input_tokens', 0)} "
        f"output={usage.get('output_tokens', 0)}]"
    )


def _format_agent_result(run) -> str:
    return run.result


tool_registry.register(ToolEntry(
    name="delegate_task",
    description=(
        "Delegate one focused task to a controlled sub-agent. Defaults to read-only tools. "
        "Use for independent analysis, research, or code inspection."
    ),
    schema={"type": "object", "properties": {
        "task": {"type": "string", "description": "The exact task to delegate."},
        "role": {"type": "string", "description": "Short role name for the sub-agent."},
        "system_prompt": {"type": "string", "description": "Optional system prompt override."},
        "tool_policy": {"type": "string", "description": "readonly, none, or all. Destructive tools remain blocked by default."},
        "max_tokens": {"type": "integer", "description": "Max output tokens."},
    }, "required": ["task"]},
    handler=_delegate_task,
    toolset="builtin",
    is_parallel_safe=True,
))

tool_registry.register(ToolEntry(
    name="run_research",
    description="Run a read-only research sub-agent for a focused question.",
    schema={"type": "object", "properties": {
        "question": {"type": "string", "description": "Research question."},
        "max_tokens": {"type": "integer", "description": "Max output tokens."},
    }, "required": ["question"]},
    handler=_run_research,
    toolset="builtin",
    is_parallel_safe=True,
))

tool_registry.register(ToolEntry(
    name="run_review",
    description="Run a read-only code review sub-agent for a target file, directory, or change.",
    schema={"type": "object", "properties": {
        "target": {"type": "string", "description": "File, directory, diff, or change description to review."},
        "focus": {"type": "string", "description": "Review focus, e.g. bugs, security, tests."},
        "max_tokens": {"type": "integer", "description": "Max output tokens."},
    }, "required": ["target"]},
    handler=_run_review,
    toolset="builtin",
    is_parallel_safe=True,
))

tool_registry.register(ToolEntry(
    name="run_workflow",
    description="Run a named workflow through the workflow engine. Args is a JSON object string.",
    schema={"type": "object", "properties": {
        "name": {"type": "string", "description": "Workflow name."},
        "args": {"type": "string", "description": "JSON object with workflow arguments."},
    }, "required": ["name"]},
    handler=_run_workflow,
    toolset="builtin",
    is_parallel_safe=False,
))

tool_registry.register(ToolEntry(
    name="sub_agent",
    description=(
        "Spawn a focused sub-agent. Call MULTIPLE TIMES in one turn to run in PARALLEL. "
        "By default sub-agents are READ-ONLY (read, grep, glob, web_search, etc.). "
        "To grant write access, set allowed_tools='[\"write\",\"edit\"]' or "
        "allowed_categories='all' for full access. "
        "Sub-agents inherit the main agent's /allow permissions for destructive tools."
    ),
    schema={"type": "object", "properties": {
        "prompt": {"type": "string", "description": "Task prompt."},
        "system_prompt": {"type": "string", "description": "Optional role/persona."},
        "schema": {"type": "string", "description": "Optional JSON schema for structured output."},
        "max_tokens": {"type": "integer", "description": "Max output tokens (default 2048)."},
        "allowed_tools": {"type": "string", "description": "JSON array of tool names to grant, e.g. '[\"write\",\"bash\"]'. Default: read-only."},
        "allowed_categories": {"type": "string", "description": "Shorthand: 'all' for full access, 'readonly' (default) for safe tools only."},
    }, "required": ["prompt"]},
    handler=_sub_agent, toolset="builtin", is_parallel_safe=True))

tool_registry.register(ToolEntry(
    name="sub_parallel",
    description="Run multiple sub-agents concurrently, wait for ALL. Tasks JSON: [{\"prompt\": \"...\"}, ...]. Total time = slowest.",
    schema={"type": "object", "properties": {
        "tasks_json": {"type": "string", "description": "JSON array of {prompt, system_prompt?, schema?}"},
    }, "required": ["tasks_json"]},
    handler=_sub_parallel, toolset="builtin", is_parallel_safe=False))

tool_registry.register(ToolEntry(
    name="sub_pipeline",
    description="Process items through a stage independently. No barrier — each item flows immediately. Use {item} as placeholder in stage_prompt.",
    schema={"type": "object", "properties": {
        "items_json": {"type": "string", "description": "JSON array of items."},
        "stage_prompt": {"type": "string", "description": "Prompt template with {item} placeholder."},
        "stage_system_prompt": {"type": "string", "description": "Optional system prompt."},
    }, "required": ["items_json", "stage_prompt"]},
    handler=_sub_pipeline, toolset="builtin", is_parallel_safe=False))
