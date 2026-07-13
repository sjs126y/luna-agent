"""Controlled sub-agent runtime."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

AGENT_RUN_SCHEMA_VERSION = 3

READONLY_TOOLS = {
    "read",
    "grep",
    "glob",
    "web_search",
    "web_fetch",
    "calculator",
    "datetime",
    "weather",
    "random",
    "json",
    "todo",
    "task",
    "process_list",
}

DESTRUCTIVE_TOOLS = {
    "write",
    "edit",
    "bash",
    "execute_code",
    "process_kill",
    "memory",
    "memory_buffer",
}

NEVER_DELEGATE = {
    "sub_agent",
    "sub_parallel",
    "sub_pipeline",
    "delegate_task",
    "run_review",
    "run_research",
    "run_workflow",
    "workflow_run",
    "clarify",
    "confirm",
}


@dataclass
class AgentSpec:
    role: str
    system_prompt: str = ""
    model: str = ""
    tool_policy: str | list[str] = "readonly"
    allowed_tools: list[str] = field(default_factory=list)
    memory_policy: str = "none"
    timeout: float = 180.0
    max_tokens: int = 2048
    output_schema: dict | None = None


@dataclass
class ToolDecision:
    name: str
    allowed: bool
    reason: str = ""
    category: str = "policy"
    phase: str = "selection"


@dataclass
class ToolCallDecision:
    call_id: str
    name: str
    allowed: bool
    reason: str = ""
    category: str = "policy"
    phase: str = "call"


@dataclass
class AgentRun:
    run_id: str
    parent_turn_id: str
    status: str
    schema_version: int = AGENT_RUN_SCHEMA_VERSION
    role: str = ""
    task: str = ""
    tool_policy: str | list[str] = "readonly"
    model: str = ""
    limits: dict[str, int | float] = field(default_factory=dict)
    granted_tools: list[str] = field(default_factory=list)
    denied_tools: list[dict] = field(default_factory=list)
    executed_tool_calls: list[dict] = field(default_factory=list)
    denied_tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    duration: float = 0.0
    started_at: str = ""
    finished_at: str = ""
    error_type: str = ""
    error_message: str = ""
    stop_requested: bool = False
    quota: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    result: str = ""


class AgentRuntime:
    """Run one explicitly delegated, bounded sub-agent task."""

    def __init__(
        self,
        *,
        call_fn: Callable | None = None,
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
        max_concurrent_runs: int = 4,
        max_tool_calls: int = 10,
        history_limit: int = 100,
        run_store_path: Path | None = None,
    ) -> None:
        self.call_fn = call_fn
        self.tools = tools or []
        self.max_tokens = max(1, int(max_tokens))
        self.max_concurrent_runs = max(1, int(max_concurrent_runs))
        self.max_tool_calls = max(0, int(max_tool_calls))
        self._runs: deque[AgentRun] = deque(maxlen=max(1, history_limit))
        self._active_runs: dict[str, AgentRun] = {}
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._run_store_path = Path(run_store_path) if run_store_path else None
        self._load_runs()

    def set_run_store(self, path: Path | None) -> None:
        self._run_store_path = Path(path) if path else None
        self._runs.clear()
        self._load_runs()

    async def run(
        self,
        task: str,
        spec: AgentSpec,
        *,
        parent_turn_id: str = "",
        allow_destructive: bool = False,
    ) -> AgentRun:
        started = time.monotonic()
        effective_max_tokens = self._clamp_max_tokens(spec.max_tokens)
        run = AgentRun(
            run_id=uuid.uuid4().hex[:12],
            parent_turn_id=parent_turn_id,
            status="running",
            role=spec.role,
            task=task,
            tool_policy=spec.tool_policy,
            model=spec.model,
            limits={
                "max_tokens": effective_max_tokens,
                "timeout": spec.timeout,
                "max_tool_calls": self.max_tool_calls,
                "max_concurrent_runs": self.max_concurrent_runs,
            },
            quota={
                "max_tokens": effective_max_tokens,
                "used_tokens": 0,
                "over_token_quota": False,
            },
            started_at=_utc_now(),
        )

        if self.call_fn is None:
            run.status = "error"
            run.error_type = "runtime"
            run.error_message = "agent runtime is not initialized"
            run.result = "Error: agent runtime is not initialized"
            self._finish_run(run, started)
            self._record_run(run)
            return run
        if len(self._active_runs) >= self.max_concurrent_runs:
            run.status = "quota_exceeded"
            run.error_type = "quota"
            run.error_message = f"max concurrent sub-agents ({self.max_concurrent_runs})"
            run.result = (
                "Error: delegated agent quota exceeded: "
                f"max concurrent sub-agents ({self.max_concurrent_runs})"
            )
            self._finish_run(run, started)
            self._record_run(run)
            return run

        current_task = asyncio.current_task()
        self._active_runs[run.run_id] = run
        if current_task is not None:
            self._active_tasks[run.run_id] = current_task

        tools, tool_decisions = self._select_tools(
            spec.tool_policy,
            allowed_tools=spec.allowed_tools,
            allow_destructive=allow_destructive,
        )
        run.granted_tools = [str(tool.get("name", "")) for tool in tools if tool.get("name")]
        run.denied_tools = [
            asdict(decision)
            for decision in tool_decisions
            if not decision.allowed
        ]
        system_prompt = spec.system_prompt or f"You are a focused {spec.role}. Complete the delegated task."
        messages = [{"role": "user", "content": [{"type": "text", "text": task}]}]
        run.messages = messages

        try:
            response = await asyncio.wait_for(
                self.call_fn(
                    messages=messages,
                    system_prompt=system_prompt,
                    tools=tools,
                    max_tokens=effective_max_tokens,
                ),
                timeout=spec.timeout,
            )
            self._accumulate_usage(run.usage, response.usage)
            if response.tool_calls:
                run.tool_calls.extend(response.tool_calls)
            if self._mark_token_quota_if_exceeded(run):
                return run
            if response.tool_calls:
                call_decisions = self._authorize_tool_calls(
                    response.tool_calls,
                    granted_tools=set(run.granted_tools),
                    allow_destructive=allow_destructive,
                )
                await self._execute_tools(
                    response,
                    messages,
                    run=run,
                    tool_authorizations=call_decisions,
                    allow_destructive=allow_destructive,
                )
                run.executed_tool_calls.extend(
                    _summarize_tool_call(call)
                    for call in response.tool_calls
                    if call_decisions.get(str(call.get("id", "")), ToolCallDecision("", "", False)).allowed
                )
                run.denied_tool_calls.extend(
                    asdict(decision)
                    for decision in call_decisions.values()
                    if not decision.allowed
                )
                response = await asyncio.wait_for(
                    self.call_fn(
                        messages=messages + [{
                            "role": "user",
                            "content": [{"type": "text", "text": "Tools are complete. Return the final answer."}],
                        }],
                        system_prompt=system_prompt,
                        tools=[],
                        max_tokens=effective_max_tokens,
                    ),
                    timeout=min(spec.timeout, 120.0),
                )
                self._accumulate_usage(run.usage, response.usage)
                if self._mark_token_quota_if_exceeded(run):
                    return run
            text = (response.text or "").strip()
            if spec.output_schema:
                text = await self._coerce_schema(
                    text,
                    spec.output_schema,
                    messages,
                    system_prompt,
                    effective_max_tokens,
                    spec.timeout,
                    run,
                )
                if self._mark_token_quota_if_exceeded(run):
                    return run
            run.status = "completed"
            run.result = text
        except asyncio.CancelledError:
            run.status = "cancelled"
            run.error_type = "cancelled"
            run.error_message = "delegated agent stopped"
            run.result = "Error: delegated agent stopped"
        except asyncio.TimeoutError:
            run.status = "timeout"
            run.error_type = "timeout"
            run.error_message = "delegated agent timed out"
            run.result = "Error: delegated agent timed out"
        except Exception as exc:
            run.status = "error"
            run.error_type = type(exc).__name__
            run.error_message = str(exc)
            run.result = f"Error: delegated agent failed: {exc}"
        finally:
            run.messages = messages
            self._active_runs.pop(run.run_id, None)
            self._active_tasks.pop(run.run_id, None)
            self._finish_run(run, started)
            self._record_run(run)
        return run

    def active_count(self) -> int:
        return len(self._active_runs)

    def list_active_runs(self) -> list[dict[str, Any]]:
        return [
            {
                "run_id": run.run_id,
                "parent_turn_id": run.parent_turn_id,
                "status": run.status,
                "role": run.role,
                "task": run.task,
                "started_at": run.started_at,
                "duration": round(_seconds_since(run.started_at), 3),
                "usage": dict(run.usage),
                "limits": dict(run.limits),
                "quota": dict(run.quota),
                "stop_requested": bool(run.stop_requested),
                "active": True,
            }
            for run in self._active_runs.values()
        ]

    def cancel_all(self) -> int:
        count = len(self._active_tasks)
        for run in self._active_runs.values():
            run.stop_requested = True
        for task in list(self._active_tasks.values()):
            task.cancel()
        return count

    def list_runs(self, *, limit: int | None = None) -> list[AgentRun]:
        runs = list(self._runs)
        if limit is not None:
            return runs[-max(0, limit):]
        return runs

    def get_run(self, run_id: str) -> AgentRun | None:
        for run in reversed(self._runs):
            if run.run_id == run_id:
                return run
        return None

    def clear_runs(self) -> None:
        self._runs.clear()
        if self._run_store_path and self._run_store_path.exists():
            self._run_store_path.unlink()

    def _clamp_max_tokens(self, requested: int) -> int:
        return max(1, min(int(requested or self.max_tokens), self.max_tokens))

    def _record_run(self, run: AgentRun) -> None:
        self._runs.append(run)
        self._append_run(run)

    def _load_runs(self) -> None:
        if self._run_store_path is None or not self._run_store_path.exists():
            return
        try:
            for line in self._run_store_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                data = json.loads(line)
                self._runs.append(_agent_run_from_dict(data))
        except Exception:
            self._runs.clear()

    def _append_run(self, run: AgentRun) -> None:
        if self._run_store_path is None:
            return
        self._run_store_path.parent.mkdir(parents=True, exist_ok=True)
        with self._run_store_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(run), ensure_ascii=False) + "\n")

    def _select_tools(
        self,
        policy: str | list[str],
        *,
        allowed_tools: list[str],
        allow_destructive: bool,
    ) -> tuple[list[dict], list[ToolDecision]]:
        allowed, policy_name = self._allowed_tool_names(policy, allowed_tools)
        selected: list[dict] = []
        decisions: list[ToolDecision] = []

        for tool in self.tools:
            name = str(tool.get("name", ""))
            if not name:
                continue
            reason, category = self._tool_denial(
                name,
                allowed=allowed,
                policy_name=policy_name,
                allow_destructive=allow_destructive,
            )
            if reason:
                decisions.append(ToolDecision(name=name, allowed=False, reason=reason, category=category))
                continue
            decisions.append(ToolDecision(name=name, allowed=True))
            selected.append(tool)

        return selected, decisions

    def _allowed_tool_names(
        self,
        policy: str | list[str],
        allowed_tools: list[str],
    ) -> tuple[set[str], str]:
        all_tool_names = {str(tool.get("name", "")) for tool in self.tools if tool.get("name")}
        if isinstance(policy, list):
            return {str(name) for name in policy}, "allowlist"

        value = str(policy or "readonly").strip()
        lowered = value.lower()
        if lowered == "none":
            return set(), "none"
        if lowered == "all":
            return all_tool_names, "all"
        if lowered == "allowlist":
            return {str(name) for name in allowed_tools}, "allowlist"
        if lowered.startswith("allowlist:"):
            names = [name.strip() for name in value.split(":", 1)[1].split(",")]
            return {name for name in names if name}, "allowlist"
        if lowered == "readonly":
            return set(READONLY_TOOLS), "readonly"
        return set(READONLY_TOOLS), "readonly"

    def _tool_denial(
        self,
        name: str,
        *,
        allowed: set[str],
        policy_name: str,
        allow_destructive: bool,
    ) -> tuple[str, str]:
        if name in NEVER_DELEGATE:
            return "recursive delegation tools are not available to sub-agents", "recursive"
        if name in DESTRUCTIVE_TOOLS and not allow_destructive:
            return "destructive tool requires explicit sub-agent authorization", "destructive"
        if self._registered_tool_is_destructive(name) and not allow_destructive:
            return "destructive tool requires explicit sub-agent authorization", "destructive"
        if name not in allowed:
            if policy_name == "none":
                return "tool_policy=none grants no tools", "policy"
            if policy_name == "allowlist":
                return "tool is not in the allowlist", "policy"
            if policy_name == "readonly":
                return "tool is not part of the readonly policy", "policy"
            return "tool is not granted by policy", "policy"
        return "", ""

    def _authorize_tool_calls(
        self,
        tool_calls: list[dict],
        *,
        granted_tools: set[str],
        allow_destructive: bool,
    ) -> dict[str, ToolCallDecision]:
        decisions: dict[str, ToolCallDecision] = {}
        for index, call in enumerate(tool_calls):
            call_id = str(call.get("id", f"tool-{index}"))
            name = str(call.get("name", ""))
            reason = ""
            category = "policy"
            if index >= self.max_tool_calls:
                reason = f"sub-agent tool call quota exceeded ({self.max_tool_calls})"
                category = "quota"
            elif not name:
                reason = "tool call has no name"
                category = "invalid"
            elif name in NEVER_DELEGATE:
                reason = "recursive delegation tools are not available to sub-agents"
                category = "recursive"
            elif name in DESTRUCTIVE_TOOLS and not allow_destructive:
                reason = "destructive tool requires explicit sub-agent authorization"
                category = "destructive"
            elif self._registered_tool_is_destructive(name) and not allow_destructive:
                reason = "destructive tool requires explicit sub-agent authorization"
                category = "destructive"
            elif name not in granted_tools:
                reason = "tool was not granted to this sub-agent"
            decisions[call_id] = ToolCallDecision(
                call_id=call_id,
                name=name,
                allowed=not reason,
                reason=reason,
                category=category,
            )
        return decisions

    def _registered_tool_is_destructive(self, name: str) -> bool:
        try:
            from personal_agent.tools.registry import tool_registry

            entry = tool_registry.get(name)
            return bool(entry and entry.is_destructive)
        except Exception:
            return False

    async def _coerce_schema(
        self,
        text: str,
        schema: dict,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int,
        timeout: float,
        run: AgentRun,
    ) -> str:
        result = _extract_json(text, schema)
        if result is not None:
            return json.dumps(result, indent=2, ensure_ascii=False)

        messages.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
        messages.append({
            "role": "user",
            "content": [{"type": "text", "text": "Return ONLY valid JSON matching the schema."}],
        })
        if self.call_fn is None:
            return f"Error: could not produce valid JSON. Raw: {text[:500]}"
        try:
            retry = await asyncio.wait_for(
                self.call_fn(
                    messages=messages,
                    system_prompt=system_prompt,
                    tools=[],
                    max_tokens=max_tokens,
                ),
                timeout=min(timeout, 60.0),
            )
            self._accumulate_usage(run.usage, retry.usage)
            retry_text = (retry.text or "").strip()
            result = _extract_json(retry_text, schema)
            if result is not None:
                return json.dumps(result, indent=2, ensure_ascii=False)
        except Exception:
            pass
        return f"Error: could not produce valid JSON. Raw: {text[:500]}"

    async def _execute_tools(
        self,
        response,
        messages: list[dict],
        *,
        run: AgentRun,
        tool_authorizations: dict[str, ToolCallDecision],
        allow_destructive: bool,
    ) -> None:
        from personal_agent.tools.executor import execute_tool_calls

        blocks = []
        if response.text:
            blocks.append({"type": "text", "text": response.text})
        for tool_call in response.tool_calls:
            blocks.append({
                "type": "tool_use",
                "id": tool_call["id"],
                "name": tool_call["name"],
                "input": tool_call["input"],
            })
        messages.append({"role": "assistant", "content": blocks})

        class _SubAgentCtx:
            _tool_calls_this_turn: int = 0
            _max_tool_calls_per_turn: int = 0
            _destructive_calls_this_turn: int = 0
            _max_destructive_per_turn: int = 3
            _security_context = None
            _security_grant_ttl_seconds: int = 0

        agent_ctx = _SubAgentCtx()
        from personal_agent.security.evaluator import isolated_security_context

        agent_ctx._security_context = isolated_security_context(
            "full-auto" if allow_destructive else "read-only"
        )
        agent_ctx._max_tool_calls_per_turn = self.max_tool_calls

        executable: list[dict] = []
        executable_ids: set[str] = set()
        result_by_id: dict[str, str] = {}
        for index, tool_call in enumerate(response.tool_calls):
            call_id = str(tool_call.get("id", f"tool-{index}"))
            decision = tool_authorizations.get(call_id)
            if decision is None or not decision.allowed:
                reason = decision.reason if decision else "tool call was not authorized"
                result_by_id[call_id] = (
                    f"Error: sub-agent tool call '{tool_call.get('name', '')}' denied: {reason}"
                )
                continue
            executable.append(tool_call)
            executable_ids.add(call_id)

        if executable:
            executed_messages: list[dict] = []
            await execute_tool_calls(executable, executed_messages, agent=agent_ctx)
            for message in reversed(executed_messages):
                if message.get("role") != "user":
                    continue
                for block in message.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        result_by_id[str(block.get("tool_use_id", ""))] = str(block.get("content", ""))
                break

        for tool_call in response.tool_calls:
            call_id = str(tool_call.get("id", ""))
            result = result_by_id.get(call_id, "Error: tool execution skipped")
            decision = tool_authorizations.get(call_id)
            if decision and decision.allowed and call_id in executable_ids and _looks_like_error(result):
                decision.allowed = False
                decision.reason = result
                decision.category = "executor"
            result_summary = {
                "id": call_id,
                "name": str(tool_call.get("name", "")),
                "input_summary": _summarize_value(tool_call.get("input", {})),
                "result_summary": _summarize_text(result),
            }
            if decision and not decision.allowed:
                result_summary["denied"] = True
                result_summary["denial_category"] = decision.category
                result_summary["denial_reason"] = decision.reason
                result_summary["denial_phase"] = decision.phase
            run.tool_results.append(result_summary)

        result_blocks = []
        for index, tool_call in enumerate(response.tool_calls):
            call_id = str(tool_call.get("id", f"tool-{index}"))
            result_blocks.append({
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": result_by_id.get(call_id, "Error: tool execution skipped"),
            })
        messages.append({"role": "user", "content": result_blocks})

    def _accumulate_usage(self, target: dict[str, int], usage: dict[str, int]) -> None:
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            if key in usage or key in target:
                target[key] = target.get(key, 0) + int(usage.get(key, 0) or 0)
        self._sync_quota_usage(target)

    def _sync_quota_usage(self, usage: dict[str, int]) -> int:
        if usage.get("total_tokens"):
            return int(usage.get("total_tokens", 0) or 0)
        return int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)

    def _mark_token_quota_if_exceeded(self, run: AgentRun) -> bool:
        used = self._sync_quota_usage(run.usage)
        max_tokens = int(run.quota.get("max_tokens", run.limits.get("max_tokens", self.max_tokens)) or self.max_tokens)
        run.quota.update({
            "max_tokens": max_tokens,
            "used_tokens": used,
            "over_token_quota": used > max_tokens,
        })
        if used <= max_tokens:
            return False
        run.status = "quota_exceeded"
        run.error_type = "quota"
        run.error_message = f"token quota exceeded ({used}/{max_tokens})"
        run.result = f"Error: delegated agent quota exceeded: token quota exceeded ({used}/{max_tokens})"
        return True

    def _finish_run(self, run: AgentRun, started: float) -> None:
        run.duration = time.monotonic() - started
        run.finished_at = _utc_now()
        if run.status in {"running", "completed"}:
            self._mark_token_quota_if_exceeded(run)
        else:
            used = self._sync_quota_usage(run.usage)
            max_tokens = int(run.quota.get("max_tokens", run.limits.get("max_tokens", self.max_tokens)) or self.max_tokens)
            run.quota.update({
                "max_tokens": max_tokens,
                "used_tokens": used,
                "over_token_quota": used > max_tokens,
            })
        run.diagnostics = self._build_diagnostics(run)

    def _build_diagnostics(self, run: AgentRun) -> dict[str, Any]:
        denial_categories = _denial_categories(run)
        return {
            "schema_version": run.schema_version,
            "status": run.status,
            "error_type": run.error_type,
            "error_message": run.error_message,
            "stop_requested": bool(run.stop_requested),
            "quota": dict(run.quota),
            "denial_categories": denial_categories,
            "tool_calls_requested": len(run.tool_calls),
            "tool_calls_executed": len(run.executed_tool_calls),
            "tool_calls_denied": len(run.denied_tool_calls),
            "tools_denied_by_policy": len(run.denied_tools),
            "tool_results": len(run.tool_results),
            "usage": dict(run.usage),
        }


def _extract_json(text: str, schema: dict) -> dict | None:
    try:
        obj = json.loads(text)
        if _validate_schema(obj, schema):
            return obj
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            if _validate_schema(obj, schema):
                return obj
        except json.JSONDecodeError:
            pass
    return None


def _summarize_tool_call(call: dict) -> dict:
    return {
        "id": str(call.get("id", "")),
        "name": str(call.get("name", "")),
        "input_summary": _summarize_value(call.get("input", {})),
    }


def _looks_like_error(text: str) -> bool:
    return str(text).strip().lower().startswith("error:")


def _summarize_value(value: Any, *, max_chars: int = 500) -> str:
    if isinstance(value, (bytes, bytearray)):
        return f"[bytes: {len(value)}]"
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(value)
    return _summarize_text(text, max_chars=max_chars)


def _summarize_text(text: str, *, max_chars: int = 500) -> str:
    text = " ".join(str(text).split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)] + "..."


def _validate_schema(obj: Any, schema: dict) -> bool:
    if schema.get("type") != "object":
        return True
    if not isinstance(obj, dict):
        return False
    return all(key in obj for key in schema.get("required", []))


def _denial_categories(run: AgentRun) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in [*run.denied_tool_calls, *run.denied_tools]:
        category = str(item.get("category", "policy") or "policy")
        counts[category] = counts.get(category, 0) + 1
    return counts


def _agent_run_from_dict(data: dict[str, Any]) -> AgentRun:
    allowed = {item.name for item in fields(AgentRun)}
    filtered = {key: value for key, value in data.items() if key in allowed}
    return AgentRun(**filtered)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seconds_since(value: str) -> float:
    if not value:
        return 0.0
    try:
        started = datetime.fromisoformat(value)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
    except ValueError:
        return 0.0
