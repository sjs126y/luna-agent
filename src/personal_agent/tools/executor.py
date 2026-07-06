"""Tool execution pipeline: scope gate → pre-hook → dispatch → post-process.
Parallel/serial execution with individual fault isolation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time as _time_module
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from personal_agent.conversation.events import emit_event
from personal_agent.tools.execution_guard import (
    GuardDecision,
    ToolDecision,
    classify_guard_denial,
    evaluate_execution_guards,
    fallback_tool_category,
    run_precheck,
    tool_decision_for_unknown_tool,
    tool_decision_from_guard,
    tool_permission_category,
)
from personal_agent.tools.registry import tool_registry

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 8000
DEFAULT_TOOL_TIMEOUT_SECONDS = 120.0

ToolExecutionStatus = Literal["success", "error", "denied", "timeout", "interrupted", "skipped"]


@dataclass
class ToolExecutionResult:
    tool_name: str
    tool_use_id: str
    status: ToolExecutionStatus
    category: str = ""
    content: str = ""
    error: str = ""
    duration: float = 0.0
    input_summary: str = ""
    output_summary: str = ""
    attempts: int = 0
    output_truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

# ── Interrupt support ────────────────────────────────
# Long-running tools (bash, execute_code) check this to abort early.
# Set by Gateway on /stop, cleared each turn.
_interrupted: bool = False
_active_tool_executions: int = 0


def set_interrupted() -> None:
    global _interrupted
    _interrupted = True


def interrupt_active_tool_executions() -> bool:
    """Interrupt running tools without leaking stop state into future calls."""
    global _interrupted
    if _active_tool_executions <= 0:
        _interrupted = False
        return False
    _interrupted = True
    return True


def clear_interrupted() -> None:
    global _interrupted
    _interrupted = False


def is_interrupted() -> bool:
    return _interrupted


async def execute_tool_calls(
    tool_calls: list[dict],
    messages: list[dict],
    *,
    agent: Any = None,
    hooks: Any = None,
    event_sink: Any = None,
    confirm: Any = None,
) -> list[ToolExecutionResult]:
    """Execute all tool calls, append results to messages in original order.

    Adjacent parallel-safe tools run concurrently; sequential tools act as
    barriers that preserve LLM ordering. Destructive tools are always barriers.
    """
    results: dict[int, ToolExecutionResult] = {}

    i = 0
    while i < len(tool_calls):
        current = tool_calls[i]
        entry = tool_registry.get(str(current.get("name", "")))

        if _can_run_in_parallel(current, entry, agent, confirm=confirm):
            # Collect adjacent safe parallel tools into a batch.
            batch: list[tuple[int, dict]] = []
            while i < len(tool_calls):
                e = tool_registry.get(str(tool_calls[i].get("name", "")))
                if _can_run_in_parallel(tool_calls[i], e, agent, confirm=confirm):
                    batch.append((i, tool_calls[i]))
                    i += 1
                else:
                    break

            gathered = await asyncio.gather(
                *[
                    execute_tool_call_result(
                        tc,
                        agent=agent,
                        hooks=hooks,
                        event_sink=event_sink,
                        confirm=confirm,
                    )
                    for _, tc in batch
                ],
                return_exceptions=True,
            )
            for j, (idx, tc) in enumerate(batch):
                item = gathered[j]
                if isinstance(item, ToolExecutionResult):
                    results[idx] = item
                elif isinstance(item, BaseException):
                    results[idx] = _result(
                        tc,
                        status="error",
                        category="executor",
                        error=f"{type(item).__name__}: {item}",
                    )
                else:
                    results[idx] = _result(tc, content=str(item))
        else:
            idx, tc = i, tool_calls[i]
            results[idx] = await execute_tool_call_result(
                tc,
                agent=agent,
                hooks=hooks,
                event_sink=event_sink,
                confirm=confirm,
            )
            i += 1

    # ── append ALL results as ONE user message (Anthropic requires this) ──
    result_blocks = []
    ordered_results: list[ToolExecutionResult] = []
    for i, tc in enumerate(tool_calls):
        result = results.get(i) or _result(tc, status="skipped", category="executor", error="tool execution skipped")
        ordered_results.append(result)
        result_blocks.append({
            "type": "tool_result",
            "tool_use_id": result.tool_use_id,
            "content": format_tool_result(result),
        })
    messages.append({"role": "user", "content": result_blocks})
    if agent is not None:
        try:
            agent._last_tool_results = [result.as_dict() for result in ordered_results]
        except Exception:
            pass
    return ordered_results


async def _exec_one(tc: dict, *, agent: Any = None, hooks: Any = None) -> str:
    """Compatibility wrapper that returns only the tool-result string."""
    return format_tool_result(await execute_tool_call_result(tc, agent=agent, hooks=hooks))


async def execute_tool_call_result(
    tc: dict,
    *,
    agent: Any = None,
    hooks: Any = None,
    event_sink: Any = None,
    confirm: Any = None,
    timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
) -> ToolExecutionResult:
    """Execute a single tool call through the security pipeline.

    Order matters — hard rejections first, then user-facing gates:
      ① pre-check — hard blocks (never ask user): bash whitelist, ext, SSRF...
      ② scope gate — may ask user: /allow for destructive tools
      ③ checkpoint — backup before destructive write
      ④ pre-hook → dispatch → post-process
    """
    started = _time_module.monotonic()
    tc = _normalize_tool_call(tc)
    name = tc["name"]
    guard_decision: GuardDecision | None = None
    tool_decision: ToolDecision | None = None
    await emit_event(
        event_sink,
        "tool_start",
        f"调用工具 {name}",
        tool_name=name,
        tool_use_id=tc["id"],
        input_summary=_summarize_value(tc.get("input", {})),
    )

    async def _finish(result: ToolExecutionResult) -> ToolExecutionResult:
        try:
            from personal_agent.tools.audit import audit_tool_result

            audit_tool_result(result, decision=tool_decision)
        except Exception:
            pass
        await emit_event(
            event_sink,
            "tool_end",
            f"工具 {result.tool_name} {result.status}",
            tool_name=result.tool_name,
            tool_use_id=result.tool_use_id,
            status=result.status,
            category=result.category,
            error=result.error,
            duration=result.duration,
            input_summary=result.input_summary,
            output_summary=result.output_summary,
            full_output=result.content or result.error,
            output_truncated=result.output_truncated,
            guard_stage=tool_decision.stage if tool_decision else "",
            guard_reason_code=tool_decision.reason_code if tool_decision else "",
            permission_category=tool_decision.permission_category if tool_decision else "",
            permission_decision=tool_decision.permission_decision if tool_decision else "",
            required_allow=tool_decision.required_allow if tool_decision else "",
            execution_mode=tool_decision.execution_mode if tool_decision else "",
            grant_matched=tool_decision.grant_matched if tool_decision else "",
            display_name=tool_decision.display_name if tool_decision else "",
            execution_mode_label=tool_decision.execution_mode_label if tool_decision else "",
            risk_level=tool_decision.risk_level if tool_decision else "",
            risk_summary=tool_decision.risk_summary if tool_decision else "",
            default_action=tool_decision.default_action if tool_decision else "",
            available_actions=list(tool_decision.available_actions) if tool_decision else [],
            input_preview=tool_decision.input_preview if tool_decision else "",
            affected_paths=list(tool_decision.affected_paths) if tool_decision else [],
            command_preview=tool_decision.command_preview if tool_decision else "",
            url_preview=tool_decision.url_preview if tool_decision else "",
            host=tool_decision.host if tool_decision else "",
            cwd=tool_decision.cwd if tool_decision else "",
            timeout_seconds=tool_decision.timeout_seconds if tool_decision else None,
            method=tool_decision.method if tool_decision else "",
            process_label=tool_decision.process_label if tool_decision else "",
        )
        return result

    if is_interrupted():
        return await _finish(_result(
            tc,
            status="interrupted",
            category="interrupt",
            error="tool execution interrupted",
            started=started,
        ))

    entry = tool_registry.get(name)
    if entry is None:
        tool_decision = tool_decision_for_unknown_tool(tc)
        await _emit_tool_decision(event_sink, tool_decision)
        return await _finish(_result(
            tc,
            status="error",
            category="unknown_tool",
            error=f"unknown tool '{name}'",
            started=started,
        ))

    # ── guard decisions: hard safety → permission → runtime guard ──
    try:
        guard_decision = evaluate_execution_guards(tc, entry, agent)
    except Exception as exc:
        return await _finish(_result(
            tc,
            status="error",
            category="execution_guard",
            error=f"{type(exc).__name__}: {exc}",
            started=started,
        ))
    tool_decision = tool_decision_from_guard(tc, guard_decision)
    if _needs_tool_confirm(tool_decision) and confirm is not None:
        answer = await _confirm_tool_decision(confirm, tool_decision, agent=agent)
        if answer == "interrupted":
            await _emit_tool_decision(event_sink, tool_decision)
            return await _finish(_result(
                tc,
                status="denied",
                category="authorization",
                error="tool confirmation interrupted",
                started=started,
            ))
        if answer in {"allow", "always"}:
            added_grants = _add_confirm_grants(
                agent,
                tool_decision,
                persist=answer == "always",
            )
            try:
                guard_decision = evaluate_execution_guards(tc, entry, agent)
            except Exception as exc:
                return await _finish(_result(
                    tc,
                    status="error",
                    category="execution_guard",
                    error=f"{type(exc).__name__}: {exc}",
                    started=started,
                ))
            finally:
                if answer == "allow":
                    _remove_confirm_grants(agent, added_grants)
            tool_decision = tool_decision_from_guard(tc, guard_decision)
    await _emit_tool_decision(event_sink, tool_decision)
    if not guard_decision.allowed:
        return await _finish(_result(
            tc,
            status="denied",
            category=classify_guard_denial(guard_decision),
            error=guard_decision.message,
            started=started,
        ))

    # ── ③ checkpoint (destructive file tools) ──────────
    if name in ("write", "edit"):
        _checkpoint_file_write(tc)

    # ── 1. pre-hook ──────────────────────────────────
    if hooks:
        try:
            hook_result = await hooks.fire("on_before_tool_exec", tc, entry)
        except Exception as exc:
            return await _finish(_result(
                tc,
                status="error",
                category="hook",
                error=f"before hook failed: {type(exc).__name__}: {exc}",
                started=started,
            ))
        if hook_result is None:
            return await _finish(_result(tc, status="denied", category="hook", error="tool execution blocked", started=started))
        if isinstance(hook_result, dict):
            modified = _normalize_tool_call(hook_result)
            if modified["name"] != name:
                return await _finish(_result(
                    tc,
                    status="denied",
                    category="hook",
                    error="before hook cannot change tool name",
                    started=started,
                ))
            tc = modified

    # ── 2. dispatch (with retry for idempotent tools) ──
    max_attempts = 2 if not entry.is_destructive else 1  # retry safe tools once
    last_exc = None
    attempts = 0
    for attempt in range(max_attempts):
        attempts = attempt + 1
        if is_interrupted():
            return await _finish(_result(
                tc,
                status="interrupted",
                category="interrupt",
                error="tool execution interrupted",
                attempts=attempts,
                started=started,
            ))
        try:
            raw_result = await _run_handler(entry.handler, tc["input"], timeout=timeout)
            result = _coerce_tool_output(raw_result)
            break
        except asyncio.TimeoutError:
            return await _finish(_result(
                tc,
                status="timeout",
                category="timeout",
                error=f"tool '{name}' timed out after {timeout:g}s",
                attempts=attempts,
                started=started,
            ))
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1 and _is_retryable(exc):
                await emit_event(
                    event_sink,
                    "retry",
                    f"工具 {name} 失败，准备重试",
                    tool_name=name,
                    attempt=attempt + 1,
                    max_attempts=max_attempts,
                    error=f"{type(exc).__name__}: {exc}",
                    recoverable=True,
                )
                logger.warning("Tool '%s' failed (attempt %d/2): %s", name, attempt + 1, exc)
                await asyncio.sleep(0.5 * (attempt + 1))  # brief backoff
                continue
            logger.exception("Tool dispatch failed for '%s'", name)
    else:
        # All attempts failed
        return await _finish(_result(
            tc,
            status="error",
            category="handler",
            error=str(last_exc or "tool dispatch failed"),
            attempts=attempts,
            started=started,
        ))

    # ── 3. post-process ──────────────────────────────
    output_truncated = False
    if len(result) > MAX_RESULT_CHARS:
        result = result[:MAX_RESULT_CHARS] + f"\n\n...({len(result) - MAX_RESULT_CHARS} more chars truncated)"
        output_truncated = True

    status: ToolExecutionStatus = "success"
    category = ""
    error = ""
    if hooks:
        try:
            modified = await hooks.fire("on_after_tool_exec", tc, result)
            if isinstance(modified, str):
                result = modified
                output_truncated = False
        except Exception as exc:
            status = "error"
            category = "hook"
            error = f"after hook failed: {type(exc).__name__}: {exc}"

    logger.debug("Tool '%s' done: %d chars", name, len(result))
    return await _finish(_result(
        tc,
        status=status,
        category=category,
        content=result,
        error=error,
        attempts=attempts,
        started=started,
        output_truncated=output_truncated,
    ))


def format_tool_result(result: ToolExecutionResult) -> str:
    if result.content:
        return result.content
    if result.error:
        return result.error if result.error.lower().startswith("error:") else f"Error: {result.error}"
    return "Error: tool execution produced no result"


def _needs_tool_confirm(decision: ToolDecision) -> bool:
    return (
        decision.stage == "permission"
        and decision.reason_code == "permission_required"
        and decision.permission_decision == "ask"
        and not decision.allowed
    )


def _can_run_in_parallel(tc: dict, entry: Any, agent: Any = None, *, confirm: Any = None) -> bool:
    if entry is None or not entry.is_parallel_safe or entry.is_destructive:
        return False
    if confirm is None:
        return True
    return not _would_need_permission_confirm(tc, entry, agent)


def _would_need_permission_confirm(tc: dict, entry: Any, agent: Any = None) -> bool:
    if agent is None:
        return False
    policy = getattr(agent, "_execution_policy", None)
    category = tool_permission_category(str(tc.get("name", "")), entry)
    decision = "ask" if entry.is_destructive else "allow"
    if policy is not None:
        decision = policy.permission_for(category)
        if decision == "allow" and entry.is_destructive and category == "default":
            decision = policy.permission_for("destructive")
    if decision != "ask":
        return False
    grants = getattr(agent, "_destructive_allowed", set())
    return "all" not in grants and category not in grants


async def _confirm_tool_decision(confirm: Any, decision: ToolDecision, *, agent: Any = None) -> str:
    if _agent_interrupt_requested(agent) or is_interrupted():
        return "interrupted"
    try:
        answer = await _await_confirm_interruptibly(confirm, decision, agent=agent)
    except Exception:
        logger.exception("Tool confirmation callback failed")
        return "deny"
    if answer == "interrupted":
        return "interrupted"
    answer_text = str(answer or "").strip().lower()
    if answer_text in {"allow", "deny", "always"}:
        return answer_text
    return "deny"


async def _await_confirm_interruptibly(confirm: Any, decision: ToolDecision, *, agent: Any = None) -> Any:
    global _active_tool_executions
    _active_tool_executions += 1
    task = asyncio.create_task(confirm(decision))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.1)
            if task in done:
                return task.result()
            if _agent_interrupt_requested(agent) or is_interrupted():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                return "interrupted"
    finally:
        _active_tool_executions = max(0, _active_tool_executions - 1)


def _agent_interrupt_requested(agent: Any) -> bool:
    return bool(getattr(agent, "_interrupt_requested", False)) if agent is not None else False


def _add_confirm_grants(agent: Any, decision: ToolDecision, *, persist: bool) -> set[str]:
    if agent is None:
        return set()
    grants = getattr(agent, "_destructive_allowed", None)
    if grants is None:
        grants = set()
        try:
            agent._destructive_allowed = grants
        except Exception:
            return set()
    category = str(decision.permission_category or "")
    required = str(decision.required_allow or "")
    tokens = {item for item in (required, category) if item}
    added: set[str] = set()
    for token in tokens:
        if token not in grants:
            grants.add(token)
            added.add(token)
    return set() if persist else added


def _remove_confirm_grants(agent: Any, added: set[str]) -> None:
    if not added or agent is None:
        return
    grants = getattr(agent, "_destructive_allowed", None)
    if grants is None:
        return
    for token in added:
        try:
            grants.discard(token)
        except AttributeError:
            return


async def _emit_tool_decision(event_sink: Any, decision: ToolDecision) -> None:
    try:
        from personal_agent.tools.audit import audit_tool_decision

        audit_tool_decision(decision)
    except Exception:
        pass
    await emit_event(
        event_sink,
        "tool_decision",
        f"工具决策 {decision.tool_name} {decision.status}",
        **decision.as_dict(),
    )


def _normalize_tool_call(tc: dict) -> dict:
    name = str(tc.get("name", ""))
    tool_use_id = str(tc.get("id") or tc.get("tool_use_id") or name or "tool")
    raw_input = tc.get("input", {})
    if raw_input is None:
        raw_input = {}
    if not isinstance(raw_input, dict):
        raw_input = {"value": raw_input}
    return {
        "id": tool_use_id,
        "name": name,
        "input": raw_input,
    }


def _result(
    tc: dict,
    *,
    status: ToolExecutionStatus = "success",
    category: str = "",
    content: str = "",
    error: str = "",
    attempts: int = 0,
    started: float | None = None,
    output_truncated: bool = False,
) -> ToolExecutionResult:
    normalized = _normalize_tool_call(tc)
    result_text = _coerce_tool_output(content) if content else ""
    error_text = _coerce_tool_output(error) if error else ""
    visible = result_text or error_text
    if len(visible) > MAX_RESULT_CHARS and not output_truncated:
        visible = visible[:MAX_RESULT_CHARS] + f"\n\n...({len(visible) - MAX_RESULT_CHARS} more chars truncated)"
        output_truncated = True
        if result_text:
            result_text = visible
        else:
            error_text = visible
    duration = 0.0 if started is None else max(0.0, _time_module.monotonic() - started)
    return ToolExecutionResult(
        tool_name=normalized["name"],
        tool_use_id=normalized["id"],
        status=status,
        category=category,
        content=result_text,
        error=error_text,
        duration=duration,
        input_summary=_summarize_value(normalized["input"]),
        output_summary=_summarize_text(visible),
        attempts=attempts,
        output_truncated=output_truncated,
    )


def _coerce_tool_output(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return f"[bytes: {len(value)}]"
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _summarize_value(value: Any, max_chars: int = 500) -> str:
    return _summarize_text(_coerce_tool_output(value), max_chars=max_chars)


def _summarize_text(value: Any, max_chars: int = 500) -> str:
    text = " ".join(str(value).split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)] + "..."


def _classify_gate_error(message: str) -> str:
    text = str(message).lower()
    if "authorization" in text or "/allow" in text:
        return "authorization"
    if "limit" in text:
        return "quota"
    if "unavailable" in text or "dependency" in text:
        return "dependency"
    return "scope_gate"


async def _run_handler(handler: Any, kwargs: dict[str, Any], *, timeout: float) -> Any:
    global _active_tool_executions
    _active_tool_executions += 1
    try:
        return await asyncio.wait_for(handler(**kwargs), timeout=timeout)
    finally:
        _active_tool_executions = max(0, _active_tool_executions - 1)


# ── pre-check: hard blocks (NEVER ask user) ─────────


def _pre_check(tc: dict, entry) -> str | None:
    """Compatibility wrapper for hard precheck rules."""
    return run_precheck(tc, entry)


# ── scope gate ────────────────────────────────────────

def _tool_category(name: str) -> str:
    """Map tool name → destructive category for granular /allow."""
    return fallback_tool_category(name)


def _scope_gate(tc: dict, entry, agent: Any) -> str | None:
    """Compatibility wrapper for execution guard checks."""
    decision = evaluate_execution_guards(tc, entry, agent)
    return None if decision.allowed else decision.message


def _looks_destructive(name: str) -> bool:
    return fallback_tool_category(name) == "write"


# ── checkpoint ────────────────────────────────────────

def _checkpoint_file_write(tc: dict) -> None:
    """Backup target file before a file_write dispatch. Best-effort — never blocks execution."""
    try:
        from personal_agent.tools.sandbox import get_sandbox

        path = tc.get("input", {}).get("path", "")
        if not path:
            return

        sandbox = get_sandbox()
        full = sandbox.resolve(path)
        if not full.exists():
            return  # new file, nothing to backup

        base = sandbox.roots[0] if sandbox.roots else Path(full).parent
        backup_dir = base / "checkpoints"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = _time_module.strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"{full.name}.{timestamp}.bak"
        shutil.copy2(full, backup_path)
        logger.info("Checkpoint saved: %s → %s", path, backup_path.name)
    except Exception:
        logger.exception("Checkpoint failed for file_write — tool execution will proceed")


def _is_retryable(exc: Exception) -> bool:
    """Check if an exception is likely transient (worth retrying once)."""
    msg = str(exc).lower()
    transient = (
        "timeout", "connection", "reset", "refused", "temporary",
        "network", "dns", "unreachable", "429", "503", "502", "504",
    )
    return any(k in msg for k in transient)
