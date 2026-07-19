"""Execution guard decisions for tool calls.

The executor is still the only tool entrypoint. This module only answers:

  - hard safety: can this ever run, or must it be blocked without asking?
  - permission: does the current mode/user grant allow it?
  - runtime guard: do dependency and quota checks allow it right now?
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import urlparse
from typing import Any, Literal

from luna_agent.tools.redact import redact

GuardStage = Literal["precheck", "permission", "runtime_guard"]
DecisionStage = Literal["lookup", "precheck", "permission", "runtime_guard", "execution"]
EXECUTION_MODE_LABELS = {
    "read-only": "Read Only",
    "ask-first": "Ask First",
    "local-auto": "Local Auto",
    "full-auto": "Full Auto",
}
MAX_PREVIEW_CHARS = 500


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
    grant_matched: str = ""
    grant_scope: str = ""
    grant_expires_at: float = 0.0
    tool_approval_mode: str = ""
    requested_resources: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class ToolDecision:
    tool_name: str
    tool_use_id: str
    allowed: bool
    stage: DecisionStage
    status: str
    permission_category: str = "default"
    execution_mode: str = ""
    permission_decision: str = ""
    reason_code: str = ""
    required_allow: str = ""
    message: str = ""
    grant_matched: str = ""
    grant_scope: str = ""
    grant_expires_at: float = 0.0
    temporary_grant_ttl_seconds: int = 0
    display_name: str = ""
    execution_mode_label: str = ""
    risk_level: str = "low"
    risk_summary: str = ""
    default_action: str = "none"
    available_actions: tuple[str, ...] = field(default_factory=tuple)
    input_summary: str = ""
    input_preview: str = ""
    affected_paths: tuple[str, ...] = field(default_factory=tuple)
    command_preview: str = ""
    url_preview: str = ""
    host: str = ""
    cwd: str = ""
    timeout_seconds: float | None = None
    method: str = ""
    process_label: str = ""
    tool_approval_mode: str = ""
    requested_resources: tuple[dict[str, str], ...] = field(default_factory=tuple)
    batch_items: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "tool_use_id": self.tool_use_id,
            "allowed": self.allowed,
            "stage": self.stage,
            "status": self.status,
            "permission_category": self.permission_category,
            "execution_mode": self.execution_mode,
            "permission_decision": self.permission_decision,
            "reason_code": self.reason_code,
            "required_allow": self.required_allow,
            "decision_message": self.message,
            "grant_matched": self.grant_matched,
            "grant_scope": self.grant_scope,
            "grant_expires_at": self.grant_expires_at,
            "temporary_grant_ttl_seconds": self.temporary_grant_ttl_seconds,
            "display_name": self.display_name,
            "execution_mode_label": self.execution_mode_label,
            "risk_level": self.risk_level,
            "risk_summary": self.risk_summary,
            "default_action": self.default_action,
            "available_actions": list(self.available_actions),
            "input_summary": self.input_summary,
            "input_preview": self.input_preview,
            "affected_paths": list(self.affected_paths),
            "command_preview": self.command_preview,
            "url_preview": self.url_preview,
            "host": self.host,
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "method": self.method,
            "process_label": self.process_label,
            "tool_approval_mode": self.tool_approval_mode,
            "requested_resources": list(self.requested_resources),
            "batch_items": list(self.batch_items),
        }


def evaluate_execution_guards(tc: dict, entry: Any, agent: Any) -> GuardDecision:
    category = tool_permission_category(str(tc.get("name", "")), entry)

    precheck = check_hard_safety(tc, entry, category)
    if not precheck.allowed:
        return precheck

    permission = check_permission(tc, entry, agent, category)
    if not permission.allowed:
        return permission

    runtime = check_runtime_guard(tc, entry, agent, category)
    if not runtime.allowed:
        return runtime

    return GuardDecision(
        stage="runtime_guard",
        allowed=True,
        category=category,
        mode=permission.mode,
        policy_decision=permission.policy_decision,
        grant_matched=permission.grant_matched,
        grant_scope=permission.grant_scope,
        grant_expires_at=permission.grant_expires_at,
        tool_approval_mode=permission.tool_approval_mode,
        requested_resources=permission.requested_resources,
    )


def tool_decision_from_guard(tc: dict, guard: GuardDecision) -> ToolDecision:
    display = build_tool_decision_display(tc, guard)
    return ToolDecision(
        tool_name=str(tc.get("name", "")),
        tool_use_id=str(tc.get("id") or tc.get("tool_use_id") or tc.get("name") or "tool"),
        allowed=guard.allowed,
        stage=guard.stage,
        status="allowed" if guard.allowed else "denied",
        permission_category=guard.category,
        execution_mode=guard.mode,
        permission_decision=guard.policy_decision,
        reason_code=guard.reason_code,
        required_allow=guard.required_allow,
        message=guard.message,
        grant_matched=guard.grant_matched,
        grant_scope=guard.grant_scope,
        grant_expires_at=guard.grant_expires_at,
        tool_approval_mode=guard.tool_approval_mode,
        requested_resources=guard.requested_resources,
        **display,
    )


def tool_decision_for_unknown_tool(tc: dict) -> ToolDecision:
    display = build_tool_decision_display(
        tc,
        GuardDecision(stage="precheck", allowed=False, reason_code="unknown_tool"),
    )
    return ToolDecision(
        tool_name=str(tc.get("name", "")),
        tool_use_id=str(tc.get("id") or tc.get("tool_use_id") or tc.get("name") or "tool"),
        allowed=False,
        stage="lookup",
        status="error",
        reason_code="unknown_tool",
        message=f"unknown tool '{tc.get('name', '')}'",
        **display,
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
        "web_search": "network",
    }
    if name in {"read", "list_directory", "file_info", "grep", "glob"}:
        return "read"
    return category_map.get(name, "write" if _looks_destructive(name) else "default")


def classify_guard_denial(decision: GuardDecision) -> str:
    if decision.stage == "precheck":
        return "precheck"
    if decision.reason_code in {"permission_required", "security_approval_required"}:
        return "authorization"
    if decision.reason_code == "quota_exceeded":
        return "quota"
    if decision.reason_code == "dependency_unavailable":
        return "dependency"
    return decision.stage


def build_tool_decision_display(tc: dict, guard: GuardDecision) -> dict[str, Any]:
    name = str(tc.get("name", "") or "tool")
    inp = _tool_input(tc)
    category = guard.category or fallback_tool_category(name)
    risk_level = _risk_level(category, guard)
    command_preview = _first_text(inp, "command", "cmd", "shell_command")
    url_preview = _first_text(inp, "url", "uri")
    host = _url_host(url_preview)
    cwd = _first_text(inp, "cwd", "work_dir", "working_dir")
    timeout_seconds = _timeout_seconds(inp)
    method = _method(inp, url_preview)
    process_label = _process_label(name, inp, command_preview=command_preview)
    affected_paths = _affected_paths(name, inp)
    input_summary = _input_summary(inp)
    input_preview = _input_preview(name, inp, command_preview=command_preview, url_preview=url_preview)
    return {
        "display_name": _display_name(name),
        "execution_mode_label": EXECUTION_MODE_LABELS.get(guard.mode, guard.mode),
        "risk_level": risk_level,
        "risk_summary": _risk_summary(category, guard, command_preview=command_preview, url_preview=url_preview),
        "default_action": _default_action(category, guard, risk_level),
        "available_actions": _available_actions(guard),
        "input_summary": input_summary,
        "input_preview": input_preview,
        "affected_paths": tuple(affected_paths),
        "command_preview": command_preview,
        "url_preview": url_preview,
        "host": host,
        "cwd": cwd,
        "timeout_seconds": timeout_seconds,
        "method": method,
        "process_label": process_label,
    }


def check_hard_safety(tc: dict, entry: Any, category: str) -> GuardDecision:
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
    try:
        from pathlib import Path
        from luna_agent.security.evaluator import prepare_tool_call
        from luna_agent.tools.sandbox import get_sandbox

        for requirement in prepare_tool_call(tc, entry).resources:
            if requirement.kind != "filesystem":
                continue
            blocked = get_sandbox().check_blocked_path(Path(requirement.resource))
            if blocked:
                return GuardDecision(
                    stage="precheck",
                    allowed=False,
                    category=category,
                    reason_code="sandbox_blocked",
                    message=blocked,
                )
    except Exception as exc:
        return GuardDecision(
            stage="precheck",
            allowed=False,
            category=category,
            reason_code="precheck_error",
            message=f"{type(exc).__name__}: {exc}",
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
            from luna_agent.tools.sandbox import get_sandbox

            full = get_sandbox().resolve(path)
            sandbox_err = get_sandbox().check_blocked_path(full)
            if sandbox_err:
                return sandbox_err

    elif name == "read":
        path = inp.get("path", "")
        if path:
            from luna_agent.tools.sandbox import get_sandbox

            full = get_sandbox().resolve(path)
            sandbox_err = get_sandbox().check_blocked_path(full)
            if sandbox_err:
                return sandbox_err

    return None


def check_permission(tc: dict, entry: Any, agent: Any, category: str) -> GuardDecision:
    security_context = getattr(agent, "_security_context", None) if agent is not None else None
    if security_context is None:
        from luna_agent.security.evaluator import unscoped_security_context

        security_context = unscoped_security_context()

    from luna_agent.security.evaluator import evaluate_tool_security, prepare_tool_call

    security = evaluate_tool_security(prepare_tool_call(tc, entry), security_context)
    preset_mode = str(getattr(security_context, "mode_id", ""))
    return GuardDecision(
        stage="permission",
        allowed=security.allowed,
        category=category,
        reason_code="" if security.allowed else security.reason_code,
        message="" if security.allowed else f"Error: {security.message}",
        mode=preset_mode,
        policy_decision=security.decision,
        required_allow="security" if security.decision == "ask" else "",
        grant_matched="tool" if security.tool_grant_matched else "",
        grant_scope="cached" if security.tool_grant_matched else "",
        tool_approval_mode=security.tool_approval_mode,
        requested_resources=tuple(item.as_dict() for item in security.missing_resources),
    )


def check_runtime_guard(tc: dict, entry: Any, agent: Any, category: str) -> GuardDecision:
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
                    "Please summarize what has been done and stop."
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


def _tool_input(tc: dict) -> dict[str, Any]:
    raw = tc.get("input", {})
    return raw if isinstance(raw, dict) else {"value": raw}


def _display_name(name: str) -> str:
    labels = {
        "write": "Write file",
        "edit": "Edit file",
        "bash": "Shell command",
        "process_start": "Start background process",
        "process_kill": "Stop background process",
        "web_search": "Web search",
    }
    return labels.get(name, name.replace("_", " ").strip().title() or "Tool")


def _risk_level(category: str, guard: GuardDecision) -> str:
    if guard.reason_code in {"hard_blacklist", "sandbox_blocked", "permission_denied"}:
        return "high"
    if category in {"bash", "background", "network", "destructive"}:
        return "high" if guard.policy_decision == "deny" else "medium"
    if category == "write":
        return "medium"
    return "low"


def _risk_summary(
    category: str,
    guard: GuardDecision,
    *,
    command_preview: str = "",
    url_preview: str = "",
) -> str:
    if guard.reason_code == "permission_denied":
        return "Execution mode denies this tool."
    if guard.reason_code in {"permission_required", "security_approval_required"}:
        return f"Execution mode requires confirmation for {category} tools."
    if guard.stage == "precheck" and guard.message:
        return _shorten(redact(guard.message), 160)
    if category == "write":
        return "May modify files in the workspace."
    if category == "bash":
        return "Will execute a shell command."
    if category == "background":
        return "Will start or manage a background process."
    if category == "network":
        return "May access the network." + (f" Target: {url_preview}" if url_preview else "")
    if command_preview:
        return "Will execute a command."
    return "Low-risk tool action."


def _default_action(category: str, guard: GuardDecision, risk_level: str) -> str:
    if guard.policy_decision == "deny" or guard.reason_code not in {"", "permission_required", "security_approval_required"}:
        return "deny"
    if guard.reason_code in {"permission_required", "security_approval_required"}:
        if category in {"bash", "background", "network", "destructive"} or risk_level == "high":
            return "none"
        return "allow"
    return "none"


def _available_actions(guard: GuardDecision) -> tuple[str, ...]:
    if guard.reason_code in {"permission_required", "security_approval_required"}:
        return ("allow_once", "allow_always", "deny")
    if guard.allowed:
        return ()
    return ("deny",)


def _affected_paths(name: str, inp: dict[str, Any]) -> list[str]:
    keys = ("path", "file", "filepath", "target", "old_path", "new_path")
    paths: list[str] = []
    if name in {"write", "edit", "read"} or any(key in inp for key in keys):
        for key in keys:
            value = inp.get(key)
            if isinstance(value, str) and value and value not in paths:
                paths.append(_shorten(redact(value), 240))
    return paths


def _first_text(inp: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            return _shorten(redact(value.strip()), 300)
    return ""


def _url_host(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    return parsed.netloc or ""


def _timeout_seconds(inp: dict[str, Any]) -> float | None:
    for key in ("timeout_seconds", "timeout"):
        value = inp.get(key)
        if isinstance(value, bool) or value in (None, ""):
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if seconds >= 0:
            return seconds
    return None


def _method(inp: dict[str, Any], url_preview: str = "") -> str:
    value = inp.get("method")
    if isinstance(value, str) and value.strip():
        return _shorten(redact(value.strip().upper()), 24)
    return "GET" if url_preview else ""


def _process_label(name: str, inp: dict[str, Any], *, command_preview: str = "") -> str:
    label = _first_text(inp, "process_label", "label", "title", "name")
    if label:
        return label
    if name.startswith("process_") and command_preview:
        return command_preview
    return ""


def _input_summary(inp: dict[str, Any]) -> str:
    if not inp:
        return "{}"
    return _shorten(_json_preview(inp), 180)


def _input_preview(
    name: str,
    inp: dict[str, Any],
    *,
    command_preview: str = "",
    url_preview: str = "",
) -> str:
    if command_preview:
        return command_preview
    if url_preview:
        return url_preview
    paths = _affected_paths(name, inp)
    if paths:
        return ", ".join(paths)
    return _shorten(_json_preview(inp), MAX_PREVIEW_CHARS)


def _json_preview(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(value)
    return redact(text)


def _shorten(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."
