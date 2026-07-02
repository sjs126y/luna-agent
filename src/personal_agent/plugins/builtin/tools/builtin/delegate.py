"""Sub-agent tools — CC-style multi-agent primitives.

sub_agent:     Spawn one sub-agent for a focused task (parallel-safe)
sub_parallel:  Run multiple sub-agents concurrently, wait for all
sub_pipeline:  Run items through stages independently (no barrier)
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Callable

from personal_agent.agents.runtime import AgentRuntime, AgentSpec
from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry

logger = logging.getLogger(__name__)

_delegate_call: Callable | None = None
_agent_runtime: AgentRuntime = AgentRuntime()
_run_store_path: Path | None = None


def setup_delegate(
    call_fn,
    tools,
    max_tokens=4096,
    run_store_path: Path | None = None,
    max_concurrent_runs: int = 4,
    max_tool_calls: int = 10,
    history_limit: int = 100,
):
    global _delegate_call, _agent_runtime, _run_store_path
    _delegate_call = call_fn
    _run_store_path = Path(run_store_path) if run_store_path else _run_store_path
    _agent_runtime = AgentRuntime(
        call_fn=call_fn,
        tools=tools,
        max_tokens=max_tokens,
        max_concurrent_runs=max_concurrent_runs,
        max_tool_calls=max_tool_calls,
        history_limit=history_limit,
        run_store_path=_run_store_path,
    )


def reset_delegate():
    global _delegate_call, _agent_runtime, _run_store_path
    _delegate_call = None
    _run_store_path = None
    _agent_runtime = AgentRuntime()


def load_agent_runs(path: Path | None) -> None:
    global _agent_runtime, _run_store_path
    _run_store_path = Path(path) if path else None
    _agent_runtime.set_run_store(_run_store_path)


def list_agent_runs(limit: int | None = None) -> list[dict]:
    return [_agent_run_summary(run) for run in _agent_runtime.list_runs(limit=limit)]


def list_active_agent_runs() -> list[dict]:
    return _agent_runtime.list_active_runs()


def get_agent_run(run_id: str):
    return _agent_runtime.get_run(run_id)


def clear_agent_runs() -> int:
    count = len(_agent_runtime.list_runs())
    _agent_runtime.clear_runs()
    return count


def stop_delegate_agents() -> int:
    return _agent_runtime.cancel_all()


def active_delegate_agents() -> int:
    return _agent_runtime.active_count()


def format_agent_runs(limit: int | None = None) -> str:
    runs = list_agent_runs(limit=limit)
    active_runs = list_active_agent_runs()
    if not runs and not active_runs:
        return "暂无子 agent 运行记录。"
    lines = []
    if active_runs:
        lines.append("运行中的子 agent:")
        for item in active_runs:
            usage = item.get("usage", {})
            lines.append(
                f"- {item['run_id']} [running] role={item.get('role') or '-'} "
                f"duration={item.get('duration', 0):.2f}s "
                f"used={item.get('quota', {}).get('used_tokens', 0)}/"
                f"{item.get('quota', {}).get('max_tokens', 0)} "
                f"stop_requested={_yes(bool(item.get('stop_requested')))} "
                f"input={usage.get('input_tokens', 0)} output={usage.get('output_tokens', 0)} "
                f"task={_shorten(item.get('task', ''), 48)}"
            )
    if runs:
        if lines:
            lines.append("")
        lines.append("子 agent 运行记录:")
    for item in runs:
        usage = item["usage"]
        result = _shorten(item.get("result", ""), 80)
        quota = item.get("quota", {})
        lines.append(
            f"- {item['run_id']} [{item['status']}] role={item['role']} "
            f"duration={item['duration']:.2f}s input={usage.get('input_tokens', 0)} "
            f"output={usage.get('output_tokens', 0)} tools={item['executed_tool_calls']} "
            f"denied={item['denied_tool_calls']} quota={quota.get('used_tokens', 0)}/"
            f"{quota.get('max_tokens', 0)} "
            f"task={_shorten(item.get('task', ''), 48)} result={result}"
        )
    return "\n".join(lines)


def format_agent_run(run_id: str) -> str:
    run = get_agent_run(run_id)
    if run is None:
        return f"未找到子 agent 运行记录: {run_id}"
    usage = run.usage
    requested_tool_names = _tool_names(run.tool_calls)
    executed_tool_names = _tool_names(run.executed_tool_calls)
    lines = [
        f"子 agent 运行: {run.run_id}",
        f"状态: {run.status} ({_status_description(run.status)})",
        f"角色: {run.role or '-'}",
        f"模型: {run.model or '-'}",
        f"工具策略: {run.tool_policy or '-'}",
        f"授予工具: {_join_or_dash(run.granted_tools)}",
        f"运行限制: {_format_limits(run.limits)}",
        f"配额: {_format_quota(run.quota)}",
        f"父 turn: {run.parent_turn_id or '-'}",
        f"开始时间: {run.started_at or '-'}",
        f"结束时间: {run.finished_at or '-'}",
        f"耗时: {run.duration:.2f}s",
        f"stop requested: {_yes(run.stop_requested)}",
        f"错误类型: {run.error_type or '-'}",
        f"错误信息: {run.error_message or '-'}",
        f"输入 tokens: {usage.get('input_tokens', 0)}",
        f"输出 tokens: {usage.get('output_tokens', 0)}",
        f"工具请求: {len(run.tool_calls)}" + (f" ({', '.join(requested_tool_names)})" if requested_tool_names else ""),
        f"已执行工具: {len(run.executed_tool_calls)}" + (f" ({', '.join(executed_tool_names)})" if executed_tool_names else ""),
        f"拒绝工具调用: {len(run.denied_tool_calls)}",
        f"拒绝分类: {_format_category_counts(run.diagnostics.get('denial_categories') or _denial_categories(run))}",
    ]
    if run.tool_results:
        lines.append(f"工具结果摘要: {len(run.tool_results)}")
        lines.extend(_format_tool_results(run.tool_results))
    if run.denied_tool_calls:
        lines.extend(_format_denials(run.denied_tool_calls))
    if run.denied_tools:
        lines.append(f"策略拒绝工具: {len(run.denied_tools)}")
        lines.extend(_format_denials(run.denied_tools[:10]))
        if len(run.denied_tools) > 10:
            lines.append(f"... 还有 {len(run.denied_tools) - 10} 个工具未展示")
    lines.extend([
        "",
        "任务:",
        run.task or "-",
        "",
        "结果:",
        run.result or "-",
    ])
    return "\n".join(lines)


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

    allowed_tools: explicit tool names to grant as an allowlist candidate.
                   Destructive tools are still blocked unless the runtime is
                   explicitly authorized by trusted code.
    allowed_categories: shortcut — "all" grants everything grantable,
                        "readonly" (default) gives only safe tools.
    """
    if _delegate_call is None:
        return "Error: sub-agent system not initialized"

    output_schema = _parse_schema(schema)
    tool_policy = _tool_policy_from_legacy(allowed_tools, allowed_categories)
    spec = AgentSpec(
        role="sub-agent",
        system_prompt=system_prompt or (
            "You are a focused sub-agent. Complete the task and return your result concisely."
        ),
        tool_policy=tool_policy,
        max_tokens=max_tokens,
        output_schema=output_schema,
    )
    run = await _agent_runtime.run(prompt, spec)
    return _format_agent_result(run)


def _tool_policy_from_legacy(allowed_tools=None, allowed_categories=None) -> str | list[str]:
    if allowed_categories == "all":
        return sorted(_READONLY_TOOLS | _GRANTABLE_TOOLS)
    allowed_tools = _normalize_allowed_tools(allowed_tools)
    if allowed_tools:
        return sorted(_READONLY_TOOLS | set(allowed_tools))
    return "readonly"


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
            return [item.strip() for item in allowed_tools.split(",") if item.strip()]
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
                         tool_policy: str = "readonly", allowed_tools: str = "",
                         max_tokens: int = 2048) -> str:
    normalized_allowed_tools = _normalize_allowed_tools(allowed_tools)
    spec = AgentSpec(
        role=role,
        system_prompt=system_prompt,
        tool_policy=_normalize_tool_policy(tool_policy, normalized_allowed_tools),
        allowed_tools=normalized_allowed_tools,
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
    denied = len(run.denied_tool_calls)
    executed = len(run.executed_tool_calls)
    return (
        f"{run.result}\n\n"
        f"[agent_run id={run.run_id} status={run.status} "
        f"duration={run.duration:.2f}s input={usage.get('input_tokens', 0)} "
        f"output={usage.get('output_tokens', 0)} tools={executed} denied={denied}]"
    )


def _format_agent_result(run) -> str:
    return run.result


def _agent_run_summary(run) -> dict:
    return {
        "schema_version": run.schema_version,
        "run_id": run.run_id,
        "parent_turn_id": run.parent_turn_id,
        "role": run.role,
        "task": run.task,
        "tool_policy": run.tool_policy,
        "model": run.model,
        "status": run.status,
        "status_description": _status_description(run.status),
        "active": False,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration": round(run.duration, 3),
        "usage": dict(run.usage),
        "limits": dict(run.limits),
        "quota": dict(run.quota),
        "error_type": run.error_type,
        "error_message": run.error_message,
        "stop_requested": run.stop_requested,
        "diagnostics": dict(run.diagnostics),
        "granted_tools": list(run.granted_tools),
        "tool_calls": len(run.tool_calls),
        "executed_tool_calls": len(run.executed_tool_calls),
        "denied_tool_calls": len(run.denied_tool_calls),
        "tool_results": len(run.tool_results),
        "denied_tools": len(run.denied_tools),
        "denial_categories": _denial_categories(run),
        "executed_tool_call_details": list(run.executed_tool_calls),
        "denied_tool_call_details": list(run.denied_tool_calls),
        "tool_result_summaries": list(run.tool_results),
        "denied_tool_selection_details": list(run.denied_tools),
        "result": run.result,
    }


def _denial_categories(run) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in [*run.denied_tool_calls, *run.denied_tools]:
        category = str(item.get("category", "policy") or "policy")
        counts[category] = counts.get(category, 0) + 1
    return counts


def _normalize_tool_policy(tool_policy: str | list[str], allowed_tools: list[str]) -> str | list[str]:
    if isinstance(tool_policy, list):
        return [str(name) for name in tool_policy]
    value = str(tool_policy or "readonly").strip()
    lowered = value.lower()
    if lowered in {"none", "readonly", "allowlist", "all"}:
        return lowered
    if lowered.startswith("allowlist:"):
        return value
    parsed_tools = _normalize_allowed_tools(value)
    if parsed_tools:
        allowed_tools[:] = parsed_tools
        return "allowlist"
    return "readonly"


def _tool_names(calls: list[dict]) -> list[str]:
    return [
        str(call.get("name", ""))
        for call in calls
        if isinstance(call, dict) and call.get("name")
    ]


def _join_or_dash(values: list[str]) -> str:
    return ", ".join(values) if values else "-"


def _format_limits(limits: dict) -> str:
    if not limits:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in limits.items())


def _format_quota(quota: dict) -> str:
    if not quota:
        return "-"
    used = quota.get("used_tokens", 0)
    max_tokens = quota.get("max_tokens", 0)
    over = " over=true" if quota.get("over_token_quota") else ""
    return f"tokens={used}/{max_tokens}{over}"


def _format_category_counts(counts: dict) -> str:
    if not counts:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _format_denials(denials: list[dict]) -> list[str]:
    lines = []
    for item in denials:
        name = str(item.get("name", ""))
        reason = str(item.get("reason", ""))
        phase = str(item.get("phase", "call"))
        category = str(item.get("category", "policy"))
        lines.append(f"  - {name or '-'} ({category}/{phase}): {reason or '-'}")
    return lines


def _format_tool_results(results: list[dict]) -> list[str]:
    lines = []
    for item in results:
        status = "denied" if item.get("denied") else "ok"
        name = str(item.get("name", ""))
        input_summary = _shorten(str(item.get("input_summary", "")), 80)
        result_summary = _shorten(str(item.get("result_summary", "")), 120)
        suffix = ""
        if item.get("denied"):
            suffix = (
                f" category={item.get('denial_category', '-')}"
                f" phase={item.get('denial_phase', '-')}"
                f" reason={_shorten(str(item.get('denial_reason', '')), 80)}"
            )
        lines.append(
            f"  - {name or '-'} [{status}] input={input_summary or '-'} "
            f"result={result_summary or '-'}{suffix}"
        )
    return lines


def _status_description(status: str) -> str:
    return {
        "completed": "已完成",
        "running": "运行中",
        "timeout": "超时",
        "cancelled": "已停止",
        "quota_exceeded": "超过配额",
        "error": "错误",
    }.get(status, "未知状态")


def _shorten(text: str, max_chars: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)] + "…"


def _yes(value: bool) -> str:
    return "是" if value else "否"


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
        "tool_policy": {"type": "string", "description": "readonly, none, allowlist, or all. Destructive tools remain blocked unless trusted runtime code explicitly authorizes them."},
        "allowed_tools": {"type": "string", "description": "JSON array or comma-separated names used when tool_policy=allowlist."},
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
        "allowed_tools narrows or extends the requested allowlist, but destructive tools "
        "remain blocked unless trusted runtime code explicitly authorizes them."
    ),
    schema={"type": "object", "properties": {
        "prompt": {"type": "string", "description": "Task prompt."},
        "system_prompt": {"type": "string", "description": "Optional role/persona."},
        "schema": {"type": "string", "description": "Optional JSON schema for structured output."},
        "max_tokens": {"type": "integer", "description": "Max output tokens (default 2048)."},
        "allowed_tools": {"type": "string", "description": "JSON array or comma-separated tool names. Destructive names are still blocked by default."},
        "allowed_categories": {"type": "string", "description": "Shorthand: 'all' requests all tools; destructive tools are still blocked by default."},
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
