"""Prepare tool calls and resolve tool/resource approval decisions."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from personal_agent.security.models import (
    FileSystemRule,
    PermissionProfile,
    PreparedToolCall,
    ResourceRequirement,
    SecurityContext,
    SecurityDecision,
)
from personal_agent.security.config import TOOL_APPROVAL_MODES
from personal_agent.security.session import SecuritySessionState


def isolated_security_context(mode_id: str = "read-only") -> SecurityContext:
    """Build an ephemeral context for internal calls without a user session."""
    from personal_agent.tools.sandbox import get_sandbox
    from personal_agent.security.modes import mode_preset

    preset = mode_preset(mode_id)
    sandbox = get_sandbox()
    if preset.profile == "read-only":
        rules = tuple(FileSystemRule(path=root, access="read") for root in sandbox.roots)
    else:
        rules = (
            *(FileSystemRule(path=root, access="write") for root in sandbox.roots),
            *(FileSystemRule(path=root, access="read") for root in sandbox.read_roots),
        )
    return SecurityContext(
        session_key="",
        profile=PermissionProfile(
            preset.profile,
            filesystem=rules,
            network_enabled=preset.profile == "trusted",
        ),
        approval_policy=preset.approval_policy,
        state=SecuritySessionState(mode_id=preset.id),
        mode_id=preset.id,
    )


def unscoped_security_context() -> SecurityContext:
    """Return the fail-closed context for calls lacking an explicit session."""
    return isolated_security_context("read-only")


def prepare_tool_call(tc: dict[str, Any], entry: Any) -> PreparedToolCall:
    name = str(tc.get("name", ""))
    inp = deepcopy(tc.get("input") or {})
    if not isinstance(inp, dict):
        inp = {}
    source = str(getattr(entry, "_plugin_key", "") or "core")
    approval_mode, approval_inherited = _approval_mode(entry, source)
    resolver = getattr(entry, "resource_resolver", None)
    if callable(resolver):
        resources = tuple(resolver(deepcopy(inp)) or ())
    else:
        resources = tuple(_builtin_resources(name, inp))
    idempotent = getattr(entry, "idempotent", None)
    if idempotent is None:
        idempotent = not bool(getattr(entry, "is_destructive", False))
    return PreparedToolCall(
        tool_use_id=str(tc.get("id") or tc.get("tool_use_id") or name or "tool"),
        name=name,
        input=inp,
        tool_key=f"{source}:{name}",
        source=source,
        approval_mode=approval_mode,
        approval_inherited=approval_inherited,
        resources=resources,
        idempotent=bool(idempotent),
        parallel_safe=bool(getattr(entry, "is_parallel_safe", False)),
    )


def evaluate_tool_security(
    prepared: PreparedToolCall,
    context: SecurityContext,
) -> SecurityDecision:
    state = context.state
    state.prune_expired()
    approval_mode = _effective_approval_mode(prepared, context)
    if approval_mode == "deny":
        return SecurityDecision(
            decision="deny",
            reason_code="tool_approval_denied",
            message=f"Tool '{prepared.name}' is disabled by local tool approval policy.",
            tool_approval_mode=approval_mode,
        )

    tool_granted = state.has_tool_grant(prepared.tool_key)
    tool_needs_approval = (
        approval_mode in {"prompt", "cached"} and not tool_granted
    )
    missing = tuple(
        requirement
        for requirement in prepared.resources
        if not context.profile.allows(requirement)
        and not state.has_resource_grant(requirement)
    )
    matched = tuple(
        requirement.key
        for requirement in prepared.resources
        if not context.profile.allows(requirement)
        and state.has_resource_grant(requirement)
    )
    if not tool_needs_approval and not missing:
        return SecurityDecision(
            decision="allow",
            reason_code="security_allowed",
            message="Tool and resource permissions are available.",
            tool_approval_mode=approval_mode,
            tool_grant_matched=tool_granted,
            resource_grants_matched=matched,
        )

    details: list[str] = []
    if tool_needs_approval:
        details.append(f"tool approval ({approval_mode})")
    details.extend(
        f"{item.access} {item.resource}" for item in missing
    )
    if context.approval_policy == "never":
        return SecurityDecision(
            decision="deny",
            reason_code="resource_permission_denied",
            message="Permission profile does not allow: " + ", ".join(details),
            tool_approval_mode=approval_mode,
            missing_resources=missing,
            tool_grant_matched=tool_granted,
            resource_grants_matched=matched,
        )
    return SecurityDecision(
        decision="ask",
        reason_code="security_approval_required",
        message="Approval required for: " + ", ".join(details),
        tool_approval_mode=approval_mode,
        missing_resources=missing,
        tool_grant_matched=tool_granted,
        resource_grants_matched=matched,
    )


def grant_prepared_call(
    prepared: PreparedToolCall,
    context: SecurityContext,
    *,
    ttl_seconds: int,
) -> tuple[set[str], set[str]]:
    tool_keys: set[str] = set()
    resource_keys: set[str] = set()
    if _effective_approval_mode(prepared, context) in {"cached", "prompt"}:
        context.state.grant_tool(prepared.tool_key, ttl_seconds=ttl_seconds)
        tool_keys.add(prepared.tool_key)
    for requirement in prepared.resources:
        if context.profile.allows(requirement):
            continue
        context.state.grant_resource(requirement, ttl_seconds=ttl_seconds)
        resource_keys.add(requirement.key)
    return tool_keys, resource_keys


def revoke_grants(
    context: SecurityContext,
    *,
    tool_keys: set[str],
    resource_keys: set[str],
) -> None:
    for key in tool_keys:
        context.state.tool_grants.pop(key, None)
    for key in resource_keys:
        context.state.resource_grants.pop(key, None)


def _approval_mode(entry: Any, source: str) -> tuple[str, bool]:
    configured = str(getattr(entry, "approval_mode", "inherit") or "inherit").strip().lower()
    if configured in TOOL_APPROVAL_MODES:
        return configured, False
    if source == "core" or source.startswith("builtin/"):
        return "auto", True
    return "cached", True


def _effective_approval_mode(
    prepared: PreparedToolCall,
    context: SecurityContext,
) -> str:
    tools = context.tool_approval_tools
    configured = tools.get(prepared.name) or tools.get(prepared.tool_key)
    if configured in TOOL_APPROVAL_MODES:
        return configured
    if prepared.name.startswith("mcp__"):
        parts = prepared.name.split("__", 2)
        server = parts[1] if len(parts) > 2 else ""
        configured = context.tool_approval_mcp_servers.get(server)
        if configured in TOOL_APPROVAL_MODES:
            return configured
    if prepared.approval_inherited and not (
        prepared.source == "core" or prepared.source.startswith("builtin/")
    ):
        return context.tool_approval_default_external
    return prepared.approval_mode


def _builtin_resources(name: str, inp: dict[str, Any]) -> list[ResourceRequirement]:
    if name in {"read", "grep", "glob", "artifact_from_file"}:
        path = str(inp.get("path") or ".")
        return [_filesystem_requirement(path, "read", name)]
    if name in {"write", "edit"}:
        path = str(inp.get("path") or "")
        return [_filesystem_requirement(path, "write", name)] if path else []
    if name == "web_fetch":
        requirement = _network_requirement(str(inp.get("url") or ""), name)
        return [requirement] if requirement else []
    if name == "web_search":
        return [ResourceRequirement("network", "web-search", "connect", "web_search")]
    return []


def _filesystem_requirement(path: str, access: str, reason: str) -> ResourceRequirement:
    from personal_agent.tools.sandbox import get_sandbox

    resolved = get_sandbox().resolve(path)
    return ResourceRequirement("filesystem", str(resolved), access, reason)


def _network_requirement(url: str, reason: str) -> ResourceRequirement | None:
    parsed = urlparse(url)
    if not parsed.hostname:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return ResourceRequirement(
        "network",
        f"{parsed.scheme or 'https'}://{parsed.hostname}:{port}",
        "connect",
        reason,
    )
