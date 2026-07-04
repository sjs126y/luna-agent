"""Execution guard decisions for tool calls.

This module keeps the executor as the single tool entrypoint while separating
hard safety checks, permission decisions, and runtime guardrails.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

GuardStage = Literal["precheck", "permission", "runtime_guard"]


@dataclass(frozen=True)
class GuardDecision:
    stage: GuardStage
    allowed: bool
    category: str = "default"
    reason_code: str = ""
    message: str = ""
    mode: str = ""
    policy_decision: str = ""
    required_allow: str = ""


def evaluate_execution_guards(tc: dict, entry: Any, agent: Any) -> GuardDecision:
    category = tool_permission_category(str(tc.get("name", "")), entry)

    precheck = _evaluate_precheck(tc, entry, category)
    if not precheck.allowed:
        return precheck

    permission = _evaluate_permission(tc, entry, agent, category)
    if not permission.allowed:
        return permission

    runtime = _evaluate_runtime_guards(tc, entry, agent, category)
    if not runtime.allowed:
        return runtime

    return GuardDecision(
        stage="runtime_guard",
        allowed=True,
        category=category,
        mode=permission.mode,
        policy_decision=permission.policy_decision,
    )


def tool_permission_category(name: str, entry: Any | None = None) -> str:
    category = ""
    if entry is not None:
        category = str(getattr(entry, "permission_category", "") or "")
    if category and category != "default":
        return category
    return fallback_tool_category(name)


def fallback_tool_category(name: str) -> str:
    category_map: dict[str, str] = {
        "write": "write",
        "edit": "write",
        "bash": "bash",
        "process_start": "background",
        "process_list": "background",
        "process_read": "background",
        "process_clear": "background",
        "process_wait": "background",
        "process_kill": "background",
        "web_fetch": "network",
        "web_search": "network",
    }
    if name in {"read", "grep", "glob"}:
        return "read"
    return category_map.get(name, "write" if _looks_destructive(name) else "default")


def classify_guard_denial(decision: GuardDecision) -> str:
    if decision.stage == "precheck":
        return "precheck"
    if decision.reason_code == "permission_required":
        return "authorization"
    if decision.reason_code == "quota_exceeded":
        return "quota"
    if decision.reason_code == "dependency_unavailable":
        return "dependency"
    return decision.stage


def _evaluate_precheck(tc: dict, entry: Any, category: str) -> GuardDecision:
    try:
        error = run_precheck(tc, entry)
    except Exception as exc:
        return GuardDecision(
            stage="precheck",
            allowed=False,
            category=category,
            reason_code="precheck_error",
            message=f"{type(exc).__name__}: {exc}",
        )
    if error:
        return GuardDecision(
            stage="precheck",
            allowed=False,
            category=category,
            reason_code=_precheck_reason_code(error),
            message=error,
        )
    return GuardDecision(stage="precheck", allowed=True, category=category)


def run_precheck(tc: dict, entry: Any) -> str | None:
    """Hard security checks that never result in user interaction."""
    name = tc["name"]
    inp = tc.get("input", {})

    if entry.precheck is not None:
        error = entry.precheck(inp)
        if error:
            return error

    if name == "edit":
        path = inp.get("path", "")
        if path:
            from personal_agent.tools.sandbox import get_sandbox

            full = get_sandbox().resolve(path)
            sandbox_err = get_sandbox().check_path(full)
            if sandbox_err:
                return sandbox_err

    elif name == "read":
        path = inp.get("path", "")
        if path:
            from personal_agent.tools.sandbox import get_sandbox

            full = get_sandbox().resolve(path)
            sandbox_err = get_sandbox().check_path(full)
            if sandbox_err:
                return sandbox_err

    elif name == "web_fetch":
        url = inp.get("url", "")
        if url:
            from personal_agent.tools.url_safety import check_url

            ssrf_err = check_url(url)
            if ssrf_err:
                return ssrf_err

    return None


def _evaluate_permission(tc: dict, entry: Any, agent: Any, category: str) -> GuardDecision:
    if agent is None:
        return GuardDecision(stage="permission", allowed=True, category=category)

    policy = getattr(agent, "_execution_policy", None)
    mode = str(getattr(policy, "mode", "")) if policy is not None else ""
    decision = "ask" if entry.is_destructive else "allow"
    if policy is not None:
        decision = policy.permission_for(category)
        if decision == "allow" and entry.is_destructive and category == "default":
            decision = policy.permission_for("destructive")

    if decision == "deny":
        return GuardDecision(
            stage="permission",
            allowed=False,
            category=category,
            reason_code="permission_denied",
            message=(
                f"Error: tool '{tc['name']}' is denied by execution mode "
                f"'{mode or 'unknown'}'."
            ),
            mode=mode,
            policy_decision=decision,
        )

    grants = getattr(agent, "_destructive_allowed", set())
    if decision == "ask" and category not in grants and "all" not in grants:
        return GuardDecision(
            stage="permission",
            allowed=False,
            category=category,
            reason_code="permission_required",
            message=(
                f"Error: tool '{tc['name']}' requires authorization. "
                f"Send /allow {category} or /allow all to enable for this turn."
            ),
            mode=mode,
            policy_decision=decision,
            required_allow=category,
        )

    return GuardDecision(
        stage="permission",
        allowed=True,
        category=category,
        mode=mode,
        policy_decision=decision,
    )


def _evaluate_runtime_guards(tc: dict, entry: Any, agent: Any, category: str) -> GuardDecision:
    if entry.check_fn and not entry.check_fn():
        return GuardDecision(
            stage="runtime_guard",
            allowed=False,
            category=category,
            reason_code="dependency_unavailable",
            message=f"Error: tool '{tc['name']}' is currently unavailable (dependency not met)",
        )

    if agent is None:
        return GuardDecision(stage="runtime_guard", allowed=True, category=category)

    if agent._tool_calls_this_turn >= agent._max_tool_calls_per_turn:
        return GuardDecision(
            stage="runtime_guard",
            allowed=False,
            category=category,
            reason_code="quota_exceeded",
            message=(
                f"Error: tool call limit ({agent._max_tool_calls_per_turn}) reached. "
                f"Please summarize what has been done and stop."
            ),
        )

    if entry.is_destructive:
        max_destructive = getattr(agent, "_max_destructive_per_turn", 3)
        destructive_count = getattr(agent, "_destructive_calls_this_turn", 0)
        if destructive_count >= max_destructive:
            return GuardDecision(
                stage="runtime_guard",
                allowed=False,
                category=category,
                reason_code="quota_exceeded",
                message=(
                    f"Error: destructive tool limit ({max_destructive}) reached. "
                    f"Please summarize or request /allow for more."
                ),
            )
        agent._destructive_calls_this_turn = destructive_count + 1

    agent._tool_calls_this_turn += 1
    return GuardDecision(stage="runtime_guard", allowed=True, category=category)


def _precheck_reason_code(message: str) -> str:
    text = str(message).lower()
    if "sandbox" in text or "outside" in text or "path blocked" in text:
        return "sandbox_blocked"
    if "network" in text:
        return "network_blocked"
    if "hard blacklist" in text or "catastrophic" in text:
        return "hard_blacklist"
    if "ssrf" in text or "url" in text:
        return "url_blocked"
    if "too large" in text or "exceed" in text or "extension" in text:
        return "input_rejected"
    return "precheck_failed"


def _looks_destructive(name: str) -> bool:
    return name in {
        "worktree_create",
        "worktree_merge",
        "worktree_cleanup",
        "task",
        "todo",
    }
