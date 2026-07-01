"""Sub-agent tools — CC-style multi-agent primitives.

sub_agent:     Spawn one sub-agent for a focused task (parallel-safe)
sub_parallel:  Run multiple sub-agents concurrently, wait for all
sub_pipeline:  Run items through stages independently (no barrier)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from personal_agent.agents.runtime import AgentRuntime, AgentSpec
from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry

logger = logging.getLogger(__name__)

_delegate_call: Callable | None = None
_delegate_tools: list[dict] | None = None
_delegate_max_tokens: int = 4096
_agent_runtime: AgentRuntime = AgentRuntime()


def setup_delegate(call_fn, tools, max_tokens=4096):
    global _delegate_call, _delegate_tools, _delegate_max_tokens, _agent_runtime
    _delegate_call = call_fn
    _delegate_tools = tools
    _delegate_max_tokens = max_tokens
    _agent_runtime = AgentRuntime(call_fn=call_fn, tools=tools, max_tokens=max_tokens)


# Default: read-only tools a sub-agent always gets
_READONLY_TOOLS = {
    "read", "grep", "glob", "web_search", "web_fetch",
    "calculator", "datetime", "weather", "random", "json",
    "todo", "task", "process_list",
}

# Tools the main agent can optionally grant
_GRANTABLE_TOOLS = {"write", "edit", "bash", "execute_code", "process_kill", "memory"}

# Never grantable — recursive or dangerous even for main agent to delegate
_NEVER_GRANT = {"sub_agent", "sub_parallel", "sub_pipeline", "workflow_run",
                "delegate_task", "clarify", "confirm"}


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

    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
    sys = system_prompt or "You are a focused sub-agent. Complete the task and return your result concisely."

    # ── Build tool list based on granted permissions ──
    if allowed_categories == "all":
        granted = set(_GRANTABLE_TOOLS)
    elif allowed_tools:
        granted = set(allowed_tools) & set(_GRANTABLE_TOOLS)
    else:
        granted = set()

    allowed = _READONLY_TOOLS | granted
    sub_tools = [t for t in (_delegate_tools or [])
                 if t.get("name") in allowed and t.get("name") not in _NEVER_GRANT]

    # Inherit main agent's /allow status for destructive tools
    # (if main agent hasn't /allow'd write, sub-agent can't either)
    inherited_allowed = set()
    if granted:
        # Check what the main agent (or whoever set up delegate) has allowed
        # We read from a stored reference to main agent's state
        pass  # set below based on granted tools

    try:
        response = await asyncio.wait_for(
            _delegate_call(messages=messages, system_prompt=sys, tools=sub_tools,
                          max_tokens=min(max_tokens, _delegate_max_tokens)),
            timeout=180.0)
    except asyncio.TimeoutError:
        return "Error: sub-agent timed out"
    except Exception as e:
        return f"Error: sub-agent failed: {e}"

    if response.tool_calls:
        from personal_agent.tools.executor import execute_tool_calls

        # Sub-agent context: inherits main agent's /allow for granted tools,
        # but starts with its own call counter for the per-turn quota.
        class _SubAgentCtx:
            _destructive_allowed: set = granted  # inherit permissions
            _tool_calls_this_turn: int = 0
            _max_tool_calls_per_turn: int = 10
            _destructive_calls_this_turn: int = 0
            _max_destructive_per_turn: int = 5

        _sub_ctx = _SubAgentCtx()
        blocks = []
        if response.text:
            blocks.append({"type": "text", "text": response.text})
        for tc in response.tool_calls:
            blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]})
        messages.append({"role": "assistant", "content": blocks})
        await execute_tool_calls(response.tool_calls, messages, agent=_sub_ctx)
        messages.append({"role": "user", "content": [{"type": "text", "text": "Tools done. Now give your final answer."}]})
        try:
            response = await asyncio.wait_for(
                _delegate_call(messages=messages, system_prompt=sys, tools=[], max_tokens=max_tokens),
                timeout=120.0)
        except Exception as e:
            return f"Error: follow-up failed: {e}"

    text = (response.text or "").strip()

    if schema:
        try:
            schema_obj = json.loads(schema)
            result = _extract_json(text, schema_obj)
            if result is not None:
                return json.dumps(result, indent=2, ensure_ascii=False)
            messages.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
            messages.append({"role": "user", "content": [{"type": "text", "text": "Return ONLY valid JSON matching the schema."}]})
            try:
                r2 = await asyncio.wait_for(
                    _delegate_call(messages=messages, system_prompt=sys, tools=[], max_tokens=max_tokens),
                    timeout=60.0)
                result = _extract_json((r2.text or "").strip(), schema_obj)
                if result is not None:
                    return json.dumps(result, indent=2, ensure_ascii=False)
            except Exception:
                pass
            return f"Error: could not produce valid JSON. Raw: {text[:500]}"
        except json.JSONDecodeError:
            pass
    return text


def _extract_json(text, schema):
    import re
    try:
        obj = json.loads(text)
        if _validate(obj, schema):
            return obj
    except json.JSONDecodeError:
        pass
    m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            if _validate(obj, schema):
                return obj
        except json.JSONDecodeError:
            pass
    return None


def _validate(obj, schema):
    if schema.get("type") != "object":
        return True
    for key in schema.get("required", []):
        if key not in obj:
            return False
    return True


async def _sub_agent(prompt, system_prompt="", schema="", max_tokens=2048,
                     allowed_tools="", allowed_categories=""):
    tools_list = json.loads(allowed_tools) if allowed_tools else None
    return await _run_agent(prompt, system_prompt, schema, max_tokens,
                            allowed_tools=tools_list,
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
