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

        if entry and entry.is_parallel_safe and not entry.is_destructive:
            # Collect adjacent safe parallel tools into a batch.
            batch: list[tuple[int, dict]] = []
            while i < len(tool_calls):
                e = tool_registry.get(str(tool_calls[i].get("name", "")))
                if e and e.is_parallel_safe and not e.is_destructive:
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
    await emit_event(
        event_sink,
        "tool_start",
        f"调用工具 {name}",
        tool_name=name,
        tool_use_id=tc["id"],
        input_summary=_summarize_value(tc.get("input", {})),
    )

    async def _finish(result: ToolExecutionResult) -> ToolExecutionResult:
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
        return await _finish(_result(
            tc,
            status="error",
            category="unknown_tool",
            error=f"unknown tool '{name}'",
            started=started,
        ))

    # ── ① pre-check: hard rejections, NEVER ask user ──
    try:
        pre_error = _pre_check(tc, entry)
    except Exception as exc:
        return await _finish(_result(
            tc,
            status="error",
            category="precheck",
            error=f"{type(exc).__name__}: {exc}",
            started=started,
        ))
    if pre_error:
        return await _finish(_result(tc, status="denied", category="precheck", error=pre_error, started=started))

    # ── ② scope gate: may ask user (/allow) ────────────
    try:
        gate_error = _scope_gate(tc, entry, agent)
    except Exception as exc:
        return await _finish(_result(
            tc,
            status="error",
            category="scope_gate",
            error=f"{type(exc).__name__}: {exc}",
            started=started,
        ))
    if gate_error:
        return await _finish(_result(
            tc,
            status="denied",
            category=_classify_gate_error(gate_error),
            error=gate_error,
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
                    error=f"{type(exc).__name__}: {exc}",
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
    if len(result) > MAX_RESULT_CHARS:
        result = result[:MAX_RESULT_CHARS] + f"\n\n...({len(result) - MAX_RESULT_CHARS} more chars truncated)"

    status: ToolExecutionStatus = "success"
    category = ""
    error = ""
    if hooks:
        try:
            modified = await hooks.fire("on_after_tool_exec", tc, result)
            if isinstance(modified, str):
                result = modified
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
    ))


def format_tool_result(result: ToolExecutionResult) -> str:
    if result.content:
        return result.content
    if result.error:
        return result.error if result.error.lower().startswith("error:") else f"Error: {result.error}"
    return "Error: tool execution produced no result"


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
) -> ToolExecutionResult:
    normalized = _normalize_tool_call(tc)
    result_text = _coerce_tool_output(content) if content else ""
    error_text = _coerce_tool_output(error) if error else ""
    visible = result_text or error_text
    if len(visible) > MAX_RESULT_CHARS:
        visible = visible[:MAX_RESULT_CHARS] + f"\n\n...({len(visible) - MAX_RESULT_CHARS} more chars truncated)"
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
    """Hard security checks that NEVER result in user interaction.

    Run BEFORE scope gate — these are unconditional rejections
    that no amount of /allow can override.
    """
    name = tc["name"]
    inp = tc.get("input", {})

    if entry.precheck is not None:
        error = entry.precheck(inp)
        if error:
            return error

    # ── edit: sandbox path check ──
    if name == "edit":
        path = inp.get("path", "")
        if path:
            from personal_agent.tools.sandbox import get_sandbox
            full = get_sandbox().resolve(path)
            sandbox_err = get_sandbox().check_path(full)
            if sandbox_err:
                return sandbox_err

    # ── read: sandbox path check (blocked patterns + roots) ──
    elif name == "read":
        path = inp.get("path", "")
        if path:
            from personal_agent.tools.sandbox import get_sandbox
            full = get_sandbox().resolve(path)
            sandbox_err = get_sandbox().check_path(full)
            if sandbox_err:
                return sandbox_err

    # ── web_fetch: SSRF prevention ──
    elif name == "web_fetch":
        url = inp.get("url", "")
        if url:
            from personal_agent.tools.url_safety import check_url
            ssrf_err = check_url(url)
            if ssrf_err:
                return ssrf_err

    return None


# ── scope gate ────────────────────────────────────────

def _tool_category(name: str) -> str:
    """Map tool name → destructive category for granular /allow."""
    _CATEGORY_MAP: dict[str, str] = {
        "write": "write",
        "edit": "write",
        "bash": "bash",
    }
    return _CATEGORY_MAP.get(name, "write")


def _scope_gate(tc: dict, entry, agent: Any) -> str | None:
    """Check if this tool call should be allowed. Returns error string or None."""

    # ① check_fn — runtime dependency check
    if entry.check_fn and not entry.check_fn():
        return f"Error: tool '{tc['name']}' is currently unavailable (dependency not met)"

    if agent is None:
        return None  # no guard checks without agent context

    # ② destructive guard — check category in allowed set
    if entry.is_destructive:
        category = _tool_category(tc["name"])
        if category not in agent._destructive_allowed and "all" not in agent._destructive_allowed:
            return (
                f"Error: destructive tool '{tc['name']}' requires authorization. "
                f"Send /allow {category} or /allow all to enable for this turn."
            )

    # ③ guardrail — per-turn call quota
    if agent._tool_calls_this_turn >= agent._max_tool_calls_per_turn:
        return (
            f"Error: tool call limit ({agent._max_tool_calls_per_turn}) reached. "
            f"Please summarize what has been done and stop."
        )

    # ③b — destructive quota (stricter, default 3 per turn)
    if entry.is_destructive:
        max_destructive = getattr(agent, '_max_destructive_per_turn', 3)
        destructive_count = getattr(agent, '_destructive_calls_this_turn', 0)
        if destructive_count >= max_destructive:
            return (
                f"Error: destructive tool limit ({max_destructive}) reached. "
                f"Please summarize or request /allow for more."
            )
        agent._destructive_calls_this_turn = destructive_count + 1

    agent._tool_calls_this_turn += 1
    return None


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
