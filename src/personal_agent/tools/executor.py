"""Tool execution pipeline: scope gate → pre-hook → dispatch → post-process.
Parallel/serial execution with individual fault isolation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time as _time_module
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from personal_agent.conversation.events import emit_event
from personal_agent.hooks import (
    HookEnvelope,
    HookEvent,
    HookScope,
    HookSourceContext,
    PermissionDecision,
)
from personal_agent.tools.execution_guard import (
    GuardDecision,
    ToolDecision,
    classify_guard_denial,
    evaluate_execution_guards,
    tool_decision_for_unknown_tool,
    tool_decision_from_guard,
    tool_permission_category,
)
from personal_agent.tools.entry import ToolArtifact, ToolHandlerOutput
from personal_agent.tools.registry import tool_registry

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 8000
DEFAULT_TOOL_TIMEOUT_SECONDS = 120.0
CHECKPOINTS_PER_FILE = 5

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
    guard_stage: str = ""
    reason_code: str = ""
    permission_category: str = ""
    permission_decision: str = ""
    required_allow: str = ""
    execution_mode: str = ""
    grant_matched: str = ""
    grant_scope: str = ""
    grant_expires_at: float = 0.0
    temporary_grant_ttl_seconds: int = 0
    artifacts: list[Any] = field(default_factory=list)
    result_metadata: dict[str, Any] = field(default_factory=dict)
    hook_feedback: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["artifacts"] = [item.safe_summary() for item in (self.artifacts or [])]
        data["result_metadata"] = _safe_result_metadata(self.result_metadata or {})
        return data


@dataclass
class _BatchConfirmation:
    confirm: Any
    security_context: Any = None
    one_time_tool_keys: set[str] = field(default_factory=set)
    one_time_resource_keys: set[str] = field(default_factory=set)

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
    event_sink: Any = None,
    confirm: Any = None,
) -> list[ToolExecutionResult]:
    """Execute all tool calls, append results to messages in original order.

    Adjacent parallel-safe tools run concurrently; sequential tools act as
    barriers that preserve LLM ordering. Destructive tools are always barriers.
    """
    tool_calls, suppressed = deduplicate_tool_calls(tool_calls)
    if suppressed:
        logger.warning(
            "Suppressed %d duplicate tool call(s) at executor boundary",
            len(suppressed),
        )
    results: dict[int, ToolExecutionResult] = {}
    batch_confirmation = await _prepare_batch_confirmation(
        tool_calls,
        agent=agent,
        confirm=confirm,
    )
    effective_confirm = batch_confirmation.confirm

    try:
        i = 0
        while i < len(tool_calls):
            current = tool_calls[i]
            entry = _resolve_tool_entry(agent, str(current.get("name", "")))

            if _can_run_in_parallel(current, entry, agent, confirm=effective_confirm):
                # Collect adjacent safe parallel tools into a batch.
                batch: list[tuple[int, dict]] = []
                while i < len(tool_calls):
                    e = _resolve_tool_entry(agent, str(tool_calls[i].get("name", "")))
                    if _can_run_in_parallel(tool_calls[i], e, agent, confirm=effective_confirm):
                        batch.append((i, tool_calls[i]))
                        i += 1
                    else:
                        break

                gathered = await asyncio.gather(
                    *[
                        execute_tool_call_result(
                            tc,
                            agent=agent,
                            event_sink=event_sink,
                            confirm=effective_confirm,
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
                    event_sink=event_sink,
                    confirm=effective_confirm,
                )
                i += 1
    finally:
        if batch_confirmation.security_context is not None and (
            batch_confirmation.one_time_tool_keys or batch_confirmation.one_time_resource_keys
        ):
            from personal_agent.security.evaluator import revoke_grants

            revoke_grants(
                batch_confirmation.security_context,
                tool_keys=batch_confirmation.one_time_tool_keys,
                resource_keys=batch_confirmation.one_time_resource_keys,
            )

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


def deduplicate_tool_calls(tool_calls: list[dict]) -> tuple[list[dict], list[dict[str, Any]]]:
    """Normalize one model batch and suppress repeated protocol call IDs."""
    unique: list[dict] = []
    suppressed: list[dict[str, Any]] = []
    seen_ids: dict[str, int] = {}

    for index, raw_call in enumerate(tool_calls or []):
        raw_id = str(raw_call.get("id") or raw_call.get("tool_use_id") or "").strip()
        normalized = _normalize_tool_call({
            **raw_call,
            "id": raw_id or f"tool-{index + 1}",
        })
        signature = _tool_call_signature(normalized)
        duplicate_index: int | None = None
        reason = ""

        if normalized["id"] in seen_ids:
            duplicate_index = seen_ids[normalized["id"]]
            previous = unique[duplicate_index]
            reason = (
                "duplicate_id"
                if _tool_call_signature(previous) == signature
                else "duplicate_id_conflict"
            )
        if duplicate_index is not None:
            kept = unique[duplicate_index]
            suppressed.append({
                "index": index,
                "tool_name": normalized["name"],
                "tool_use_id": normalized["id"],
                "reason": reason,
                "kept_tool_use_id": kept["id"],
            })
            continue

        seen_ids[normalized["id"]] = len(unique)
        unique.append(normalized)

    return unique, suppressed


async def _prepare_batch_confirmation(
    tool_calls: list[dict],
    *,
    agent: Any,
    confirm: Any,
) -> _BatchConfirmation:
    if confirm is None or agent is None:
        return _BatchConfirmation(confirm=confirm)
    security_context = getattr(agent, "_security_context", None)
    if security_context is None:
        return _BatchConfirmation(confirm=confirm)

    from personal_agent.security.evaluator import grant_prepared_call, prepare_tool_call

    candidates: list[tuple[dict, Any, ToolDecision]] = []
    for raw_call in tool_calls:
        tc = _normalize_tool_call(raw_call)
        entry = _resolve_tool_entry(agent, tc["name"])
        if entry is None or _has_matching_tool_policy_hook(agent, tc, entry):
            continue
        try:
            guard = evaluate_execution_guards(tc, entry, agent)
        except Exception:
            continue
        decision = _with_agent_grant_metadata(tool_decision_from_guard(tc, guard), agent)
        if _needs_tool_confirm(decision):
            candidates.append((tc, entry, decision))

    if len(candidates) < 2:
        return _BatchConfirmation(confirm=confirm)

    aggregate = _aggregate_confirmation_decision([item[2] for item in candidates])
    answer = await _confirm_tool_decision(confirm, aggregate, agent=agent)
    candidate_ids = {item[0]["id"] for item in candidates}

    async def effective_confirm(decision: ToolDecision) -> str:
        if decision.tool_use_id in candidate_ids:
            return answer
        return await confirm(decision)

    batch = _BatchConfirmation(confirm=effective_confirm, security_context=security_context)
    if answer not in {"allow", "always"}:
        return batch

    ttl = int(getattr(agent, "_security_grant_ttl_seconds", 3600) or 3600)
    for tc, entry, decision in candidates:
        one_time = answer == "allow" or decision.tool_approval_mode == "prompt"
        granted_tools, granted_resources = grant_prepared_call(
            prepare_tool_call(tc, entry),
            security_context,
            ttl_seconds=60 if one_time else ttl,
        )
        if one_time:
            batch.one_time_tool_keys.update(granted_tools)
            batch.one_time_resource_keys.update(granted_resources)
    return batch


def _has_matching_tool_policy_hook(agent: Any, tc: dict, entry: Any) -> bool:
    manager = getattr(agent, "_hook_manager", None)
    if manager is None or not hasattr(manager, "has_matching_registration"):
        return False
    for event in (HookEvent.PRE_TOOL_USE, HookEvent.PERMISSION_REQUEST):
        envelope = _tool_hook_envelope(agent, event, tc, entry=entry)
        if manager.has_matching_registration(envelope):
            return True
    return False


def _aggregate_confirmation_decision(decisions: list[ToolDecision]) -> ToolDecision:
    first = decisions[0]
    resources: dict[tuple[str, str, str], dict[str, str]] = {}
    for decision in decisions:
        for resource in decision.requested_resources:
            key = (
                str(resource.get("kind") or ""),
                str(resource.get("access") or ""),
                str(resource.get("resource") or ""),
            )
            resources[key] = resource
    return replace(
        first,
        tool_name="batch",
        tool_use_id="batch:" + ",".join(item.tool_use_id for item in decisions),
        display_name=f"{len(decisions)} 个工具调用",
        permission_category="batch",
        input_summary="",
        input_preview="",
        affected_paths=tuple(
            path for item in decisions for path in item.affected_paths
        ),
        requested_resources=tuple(resources.values()),
        batch_items=tuple(item.as_dict() for item in decisions),
    )


async def _exec_one(tc: dict, *, agent: Any = None) -> str:
    """Compatibility wrapper that returns only the tool-result string."""
    return format_tool_result(await execute_tool_call_result(tc, agent=agent))


def _resolve_tool_entry(agent: Any, name: str):
    if agent is not None:
        route = getattr(agent, "_tool_bindings", {}).get(name)
        manager = getattr(agent, "_plugin_manager", None)
        if route is not None and manager is not None:
            entry = manager.capability_payload(route.binding_id)
            if entry is not None:
                return entry
            return None
    return tool_registry.get(name)


async def execute_tool_call_result(
    tc: dict,
    *,
    agent: Any = None,
    event_sink: Any = None,
    confirm: Any = None,
    timeout: float | None = None,
) -> ToolExecutionResult:
    """Execute a single tool call through the security pipeline.

    Order matters — hard rejections first, then user-facing gates:
      ① typed proposal hook — may block or rewrite input
      ② pre-check — hard blocks (never ask user): bash whitelist, ext, SSRF...
      ③ security gate — may ask for exact tool/resource approval
      ④ checkpoint → dispatch → post-process
    """
    started = _time_module.monotonic()
    tc = _normalize_tool_call(tc)
    name = tc["name"]
    entry = _resolve_tool_entry(agent, name)
    execution_timeout = _resolve_tool_timeout(entry, tc["input"], timeout)
    guard_decision: GuardDecision | None = None
    tool_decision: ToolDecision | None = None
    one_time_security_context = None
    one_time_security_grants: tuple[set[str], set[str]] = (set(), set())
    preserve_nested_guard = False
    await emit_event(
        event_sink,
        "tool_start",
        f"调用工具 {name}",
        tool_name=name,
        tool_use_id=tc["id"],
        input_summary=_summarize_value(tc.get("input", {})),
    )

    async def _finish(result: ToolExecutionResult) -> ToolExecutionResult:
        nonlocal one_time_security_grants
        if tool_decision is not None and not preserve_nested_guard:
            if not result.guard_stage:
                result.guard_stage = tool_decision.stage
            if not result.reason_code:
                result.reason_code = tool_decision.reason_code
            result.permission_category = tool_decision.permission_category
            result.permission_decision = tool_decision.permission_decision
            result.required_allow = tool_decision.required_allow
            result.execution_mode = tool_decision.execution_mode
            result.grant_matched = tool_decision.grant_matched
            result.grant_scope = tool_decision.grant_scope
            result.grant_expires_at = tool_decision.grant_expires_at
            result.temporary_grant_ttl_seconds = tool_decision.temporary_grant_ttl_seconds
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
            guard_stage=result.guard_stage,
            guard_reason_code=result.reason_code,
            permission_category=result.permission_category,
            permission_decision=result.permission_decision,
            required_allow=result.required_allow,
            execution_mode=result.execution_mode,
            grant_matched=result.grant_matched,
            grant_scope=result.grant_scope,
            grant_expires_at=result.grant_expires_at,
            temporary_grant_ttl_seconds=result.temporary_grant_ttl_seconds,
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
            tool_approval_mode=tool_decision.tool_approval_mode if tool_decision else "",
            requested_resources=list(tool_decision.requested_resources) if tool_decision else [],
            artifact_count=len(result.artifacts or []),
            artifacts=[item.safe_summary() for item in (result.artifacts or [])],
            result_metadata=_safe_result_metadata(result.result_metadata or {}),
            count_as_tool=bool(entry is None or entry.report_as_tool),
        )
        stored_summaries = [
            item.safe_summary()
            for item in (result.artifacts or [])
            if getattr(item, "artifact_id", "")
        ]
        if stored_summaries:
            await emit_event(
                event_sink,
                "artifact_available",
                "工具产物已保存",
                tool_name=result.tool_name,
                tool_use_id=result.tool_use_id,
                artifacts=stored_summaries,
            )
        selected_ids = list((result.result_metadata or {}).get("selected_artifact_ids") or [])
        if selected_ids and result.status == "success":
            await emit_event(
                event_sink,
                "response_artifact_selected",
                "已选择回复附件",
                artifact_ids=[str(value) for value in selected_ids[:10]],
                count=len(selected_ids),
            )
        if one_time_security_context is not None and any(one_time_security_grants):
            from personal_agent.security.evaluator import revoke_grants

            revoke_grants(
                one_time_security_context,
                tool_keys=one_time_security_grants[0],
                resource_keys=one_time_security_grants[1],
            )
            one_time_security_grants = (set(), set())
        return result

    if is_interrupted():
        return await _finish(_result(
            tc,
            status="interrupted",
            category="interrupt",
            error="tool execution interrupted",
            started=started,
        ))

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

    hook_manager = getattr(agent, "_hook_manager", None) if agent is not None else None
    if hook_manager is not None:
        pre_outcome = await hook_manager.dispatch(_tool_hook_envelope(
            agent,
            HookEvent.PRE_TOOL_USE,
            tc,
            entry=entry,
        ))
        _record_hook_context(agent, getattr(pre_outcome, "additional_context", ""))
        if getattr(pre_outcome, "blocked", False):
            return await _finish(_result(
                tc,
                status="denied",
                category="hook",
                error=getattr(pre_outcome, "reason", "") or "tool execution blocked by hook",
                started=started,
            ))
        updated_input = getattr(pre_outcome, "updated_input", None)
        if updated_input is not None:
            tc = _normalize_tool_call({**tc, "input": dict(updated_input)})

    # Evaluate all guards against the final hook-approved input.
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
    tool_decision = _with_agent_grant_metadata(tool_decision_from_guard(tc, guard_decision), agent)
    hook_permission = PermissionDecision.ABSTAIN
    if _needs_tool_confirm(tool_decision) and hook_manager is not None:
        permission_outcome = await hook_manager.dispatch(_tool_hook_envelope(
            agent,
            HookEvent.PERMISSION_REQUEST,
            tc,
            entry=entry,
            tool_decision=tool_decision,
        ))
        hook_permission = getattr(permission_outcome, "decision", PermissionDecision.ABSTAIN)
        if hook_permission == PermissionDecision.DENY:
            await _emit_tool_decision(event_sink, tool_decision)
            return await _finish(_result(
                tc,
                status="denied",
                category="hook",
                error=getattr(permission_outcome, "reason", "") or "permission denied by hook",
                started=started,
            ))
    if _needs_tool_confirm(tool_decision) and (
        hook_permission == PermissionDecision.ALLOW or confirm is not None
    ):
        answer = (
            "allow"
            if hook_permission == PermissionDecision.ALLOW
            else await _confirm_tool_decision(confirm, tool_decision, agent=agent)
        )
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
            security_context = getattr(agent, "_security_context", None) if agent is not None else None
            if security_context is not None and tool_decision.reason_code == "security_approval_required":
                from personal_agent.security.evaluator import grant_prepared_call, prepare_tool_call

                ttl = int(getattr(agent, "_security_grant_ttl_seconds", 3600) or 3600)
                one_time = answer == "allow" or tool_decision.tool_approval_mode == "prompt"
                if one_time:
                    ttl = 60
                security_grants = grant_prepared_call(
                    prepare_tool_call(tc, entry), security_context, ttl_seconds=ttl
                )
                if one_time:
                    one_time_security_context = security_context
                    one_time_security_grants = security_grants
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
            tool_decision = _with_agent_grant_metadata(tool_decision_from_guard(tc, guard_decision), agent)
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

    # ── 2. dispatch (with retry for idempotent tools) ──
    from personal_agent.security.evaluator import prepare_tool_call

    prepared = prepare_tool_call(tc, entry)
    max_attempts = 2 if prepared.idempotent else 1
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
            raw_result = await _run_handler(
                entry.handler,
                tc["input"],
                timeout=execution_timeout,
                agent=agent,
                confirm=confirm,
                event_sink=event_sink,
            )
            nested_result = raw_result if isinstance(raw_result, ToolExecutionResult) else None
            if nested_result is not None:
                handler_output = ToolHandlerOutput(
                    text=nested_result.content or nested_result.error,
                    artifacts=list(nested_result.artifacts),
                    metadata={
                        **nested_result.result_metadata,
                    },
                    is_error=nested_result.status != "success",
                )
            else:
                handler_output = _normalize_handler_output(raw_result)
            result = handler_output.text
            break
        except asyncio.TimeoutError:
            return await _finish(_result(
                tc,
                status="timeout",
                category="timeout",
                error=f"tool '{name}' timed out after {execution_timeout:g}s",
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
    materialized_artifacts = list(handler_output.artifacts)
    artifact_warnings: list[str] = []
    artifact_store = getattr(agent, "_artifact_store", None) if agent is not None else None
    if artifact_store is not None and materialized_artifacts and nested_result is None:
        from personal_agent.artifacts import ArtifactStoreError, materialize_tool_artifact

        stored = []
        for artifact in materialized_artifacts:
            try:
                stored.append(await materialize_tool_artifact(
                    artifact_store,
                    artifact,
                    session_key=str(
                        getattr(getattr(agent, "_security_context", None), "session_key", "")
                        or getattr(agent, "_memory_session_key", "")
                    ),
                    turn_id=str(getattr(agent, "_hook_turn_id", "") or ""),
                    tool_name=name,
                    result_metadata=handler_output.metadata,
                ))
            except ArtifactStoreError as exc:
                artifact_warnings.append(f"{exc.reason}: {exc.detail or str(exc)}")
        materialized_artifacts = stored
        if artifact_warnings:
            warning = "; ".join(artifact_warnings)
            result = f"{result}\n\n[artifact unavailable: {warning}]".strip()
    output_truncated = False
    if len(result) > MAX_RESULT_CHARS:
        result = result[:MAX_RESULT_CHARS] + f"\n\n...({len(result) - MAX_RESULT_CHARS} more chars truncated)"
        output_truncated = True

    status: ToolExecutionStatus = "success"
    category = ""
    error = ""
    handler_reason_code = ""
    if nested_result is not None:
        status = nested_result.status
        category = nested_result.category
        error = nested_result.error
        if status != "success":
            error = error or result or f"nested tool '{nested_result.tool_name}' failed"
            result = ""
    elif handler_output.is_error:
        handler_reason_code = str(handler_output.metadata.get("reason_code") or "")
        status = "denied" if handler_reason_code in {"sandbox_blocked", "hard_blacklist"} else "error"
        category = "precheck" if status == "denied" else "handler"
        error = result or "MCP tool call failed"
        result = ""
    logger.debug("Tool '%s' done: %d chars", name, len(result))
    final_result = _result(
        tc,
        status=status,
        category=category,
        content=result,
        error=error,
        artifacts=materialized_artifacts,
        result_metadata=handler_output.metadata,
        attempts=attempts,
        started=started,
        output_truncated=output_truncated,
    )
    if handler_reason_code:
        final_result.guard_stage = "runtime_guard"
        final_result.reason_code = handler_reason_code
    if nested_result is not None:
        preserve_nested_guard = True
        final_result.guard_stage = nested_result.guard_stage
        final_result.reason_code = nested_result.reason_code
        final_result.permission_category = nested_result.permission_category
        final_result.permission_decision = nested_result.permission_decision
        final_result.required_allow = nested_result.required_allow
        final_result.execution_mode = nested_result.execution_mode
        final_result.grant_matched = nested_result.grant_matched
        final_result.grant_scope = nested_result.grant_scope
        final_result.grant_expires_at = nested_result.grant_expires_at
        final_result.temporary_grant_ttl_seconds = nested_result.temporary_grant_ttl_seconds
    if hook_manager is not None:
        post_outcome = await hook_manager.dispatch(_tool_hook_envelope(
            agent,
            HookEvent.POST_TOOL_USE,
            tc,
            entry=entry,
            tool_result=final_result,
        ))
        _record_hook_context(agent, getattr(post_outcome, "additional_context", ""))
        if getattr(post_outcome, "blocked", False):
            final_result.hook_feedback = (
                getattr(post_outcome, "reason", "")
                or "Tool result requires review before continuing."
            )
    return await _finish(final_result)


def format_tool_result(result: ToolExecutionResult) -> str:
    if result.hook_feedback:
        return result.hook_feedback
    if result.content:
        visible = result.content
        summaries = [
            item.safe_summary()
            for item in (result.artifacts or [])
            if getattr(item, "artifact_id", "")
        ]
        if summaries:
            visible += "\n\nAvailable response artifacts:\n" + json.dumps(
                summaries,
                ensure_ascii=False,
                sort_keys=True,
            )
        return visible
    if result.error:
        return result.error if result.error.lower().startswith("error:") else f"Error: {result.error}"
    return "Error: tool execution produced no result"


def _needs_tool_confirm(decision: ToolDecision) -> bool:
    return (
        decision.stage == "permission"
        and decision.reason_code in {"permission_required", "security_approval_required"}
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
    from personal_agent.security.evaluator import (
        evaluate_tool_security,
        prepare_tool_call,
        unscoped_security_context,
    )

    context = getattr(agent, "_security_context", None) if agent is not None else None
    context = context or unscoped_security_context()
    return evaluate_tool_security(prepare_tool_call(tc, entry), context).decision == "ask"


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


def _with_agent_grant_metadata(decision: ToolDecision, agent: Any) -> ToolDecision:
    if agent is None:
        return decision
    ttl = int(getattr(agent, "_security_grant_ttl_seconds", 0) or 0)
    if not ttl:
        return decision
    return replace(decision, temporary_grant_ttl_seconds=ttl)


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


def _tool_hook_envelope(
    agent: Any,
    event: HookEvent,
    tool_call: dict,
    *,
    entry: Any = None,
    tool_decision: ToolDecision | None = None,
    tool_result: ToolExecutionResult | None = None,
) -> HookEnvelope:
    security_context = getattr(agent, "_security_context", None)
    source = getattr(agent, "_hook_source", None)
    payload: dict[str, Any] = {
        "tool_name": str(tool_call.get("name") or ""),
        "tool_use_id": str(tool_call.get("id") or ""),
        "tool_input": dict(tool_call.get("input") or {}),
        "display_name": str(getattr(entry, "display_name", "") or ""),
    }
    if tool_decision is not None:
        payload.update({
            "requested_resources": list(tool_decision.requested_resources),
            "risk_level": tool_decision.risk_level,
            "risk_summary": tool_decision.risk_summary,
            "approval_mode": tool_decision.tool_approval_mode,
        })
    if tool_result is not None:
        payload["tool_result"] = tool_result.as_dict()
    return HookEnvelope(
        event_name=event,
        scope=HookScope.TURN,
        session_key=str(
            getattr(security_context, "session_key", "")
            or getattr(agent, "_memory_session_key", "")
        ),
        turn_id=str(getattr(agent, "_hook_turn_id", "") or ""),
        agent_id="main",
        cwd=str(payload["tool_input"].get("cwd") or Path.cwd()),
        mode=str(getattr(security_context, "mode_id", "") or ""),
        source=HookSourceContext(
            platform=str(getattr(source, "platform", "") or ""),
            user_id=str(getattr(source, "user_id", "") or ""),
            chat_id=str(getattr(source, "chat_id", "") or ""),
        ),
        payload=payload,
    )


def _record_hook_context(agent: Any, value: str) -> None:
    text = str(value or "").strip()
    if not text or agent is None:
        return
    contexts = getattr(agent, "_hook_additional_contexts", None)
    if isinstance(contexts, list):
        contexts.append(f"[Hook additional context]\n{text}")


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


def _tool_call_signature(tc: dict) -> str:
    return json.dumps(
        {
            "name": str(tc.get("name") or ""),
            "input": tc.get("input") or {},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


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
    artifacts: list[Any] | None = None,
    result_metadata: dict[str, Any] | None = None,
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
        artifacts=list(artifacts or []),
        result_metadata=dict(result_metadata or {}),
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


def _normalize_handler_output(value: Any) -> ToolHandlerOutput:
    if isinstance(value, ToolHandlerOutput):
        return ToolHandlerOutput(
            text=_coerce_tool_output(value.text),
            artifacts=list(value.artifacts),
            metadata=dict(value.metadata),
            is_error=bool(value.is_error),
        )
    return ToolHandlerOutput(text=_coerce_tool_output(value))


def _safe_result_metadata(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key == "structured_content":
            result["structured_content_present"] = item is not None
        elif key in {"mcp_server", "remote_tool"}:
            result[key] = str(item)[:200]
        elif isinstance(item, (bool, int, float)) or item is None:
            result[str(key)[:100]] = item
        elif key == "selected_artifact_ids" and isinstance(item, list):
            result[key] = [str(value)[:100] for value in item[:10]]
    return result


def _summarize_value(value: Any, max_chars: int = 500) -> str:
    return _summarize_text(_coerce_tool_output(value), max_chars=max_chars)


def _summarize_text(value: Any, max_chars: int = 500) -> str:
    text = " ".join(str(value).split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)] + "..."


async def _run_handler(
    handler: Any,
    kwargs: dict[str, Any],
    *,
    timeout: float,
    agent: Any = None,
    confirm: Any = None,
    event_sink: Any = None,
) -> Any:
    global _active_tool_executions
    from personal_agent.tools.runtime_context import (
        reset_current_tool_agent,
        reset_current_tool_confirm,
        reset_current_tool_event_sink,
        set_current_tool_agent,
        set_current_tool_confirm,
        set_current_tool_event_sink,
    )

    _active_tool_executions += 1
    agent_token = set_current_tool_agent(agent)
    confirm_token = set_current_tool_confirm(confirm)
    event_sink_token = set_current_tool_event_sink(event_sink)
    try:
        return await asyncio.wait_for(handler(**kwargs), timeout=timeout)
    finally:
        reset_current_tool_event_sink(event_sink_token)
        reset_current_tool_confirm(confirm_token)
        reset_current_tool_agent(agent_token)
        _active_tool_executions = max(0, _active_tool_executions - 1)


def _resolve_tool_timeout(entry: Any, tool_input: dict[str, Any], explicit: float | None) -> float:
    if explicit is not None:
        value = float(explicit)
        return value if value > 0 else DEFAULT_TOOL_TIMEOUT_SECONDS
    if entry is not None:
        resolver = getattr(entry, "timeout_resolver", None)
        if callable(resolver):
            try:
                value = resolver(dict(tool_input or {}))
                if value is not None and float(value) > 0:
                    return float(value)
            except Exception:
                logger.exception("Tool timeout resolver failed for '%s'", entry.name)
        value = getattr(entry, "timeout_seconds", None)
        if value is not None and float(value) > 0:
            return float(value)
    return DEFAULT_TOOL_TIMEOUT_SECONDS


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
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_path = backup_dir / f"{full.name}.{timestamp}.bak"
        shutil.copy2(full, backup_path)
        backups = sorted(
            item for item in backup_dir.iterdir()
            if item.is_file()
            and item.name.startswith(f"{full.name}.")
            and item.name.endswith(".bak")
        )
        for stale in backups[:-CHECKPOINTS_PER_FILE]:
            stale.unlink(missing_ok=True)
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
