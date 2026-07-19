"""Validation and normalization for tool approval settings."""

from __future__ import annotations

from typing import Any

TOOL_APPROVAL_MODES = {"auto", "cached", "prompt", "deny"}

APPROVAL_REVIEWER_FALLBACKS = {"human", "deny"}
APPROVAL_REVIEWER_RISK_LEVELS = {"low", "medium"}


def normalize_approval_reviewer_config(raw: Any) -> dict[str, Any]:
    """Normalize the optional model-backed approval reviewer settings."""
    value = raw if isinstance(raw, dict) else {}
    fallback = str(value.get("fallback") or "human").strip().lower()
    max_risk = str(value.get("max_risk") or "medium").strip().lower()
    timeout = value.get("timeout_seconds", 12)
    try:
        timeout = max(1, min(60, int(timeout)))
    except (TypeError, ValueError):
        timeout = 12
    model = str(value.get("model") or "").strip()
    return {
        "enabled": bool(value.get("enabled", False)),
        "model": model,
        "timeout_seconds": timeout,
        "fallback": fallback if fallback in APPROVAL_REVIEWER_FALLBACKS else "human",
        # TTL grants remain user-only in the first reviewer implementation.
        "allow_ttl_grant": False,
        "max_risk": max_risk if max_risk in APPROVAL_REVIEWER_RISK_LEVELS else "medium",
    }


def approval_reviewer_config_errors(
    raw: Any,
    *,
    label: str = "permissions.approval_reviewer",
) -> list[str]:
    if not isinstance(raw, dict):
        return [f"{label} 必须是对象。"]
    errors: list[str] = []
    allowed_keys = {"enabled", "model", "timeout_seconds", "fallback", "allow_ttl_grant", "max_risk"}
    for key in sorted(raw):
        if key not in allowed_keys:
            errors.append(f"{label}.{key} 不支持。")
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        errors.append(f"{label}.enabled 必须是 true/false。")
    if "model" in raw and not isinstance(raw["model"], str):
        errors.append(f"{label}.model 必须是字符串。")
    if "timeout_seconds" in raw:
        try:
            timeout = int(raw["timeout_seconds"])
        except (TypeError, ValueError):
            timeout = 0
        if not 1 <= timeout <= 60:
            errors.append(f"{label}.timeout_seconds 必须在 1 到 60 之间。")
    if "fallback" in raw and str(raw["fallback"]).strip().lower() not in APPROVAL_REVIEWER_FALLBACKS:
        errors.append(f"{label}.fallback 必须是 human 或 deny。")
    if "allow_ttl_grant" in raw and raw["allow_ttl_grant"] not in {False, None}:
        errors.append(f"{label}.allow_ttl_grant 当前必须为 false。")
    if "max_risk" in raw and str(raw["max_risk"]).strip().lower() not in APPROVAL_REVIEWER_RISK_LEVELS:
        errors.append(f"{label}.max_risk 当前必须是 low 或 medium。")
    return errors


def normalize_tool_approval_config(raw: Any) -> dict[str, Any]:
    value = raw if isinstance(raw, dict) else {}

    def modes(key: str) -> dict[str, str]:
        configured = value.get(key) or {}
        if not isinstance(configured, dict):
            return {}
        return {
            str(name): str(mode).strip().lower()
            for name, mode in configured.items()
            if str(mode).strip().lower() in TOOL_APPROVAL_MODES
        }

    default_external = str(value.get("default_external") or "cached").strip().lower()
    return {
        "default_external": (
            default_external if default_external in TOOL_APPROVAL_MODES else "cached"
        ),
        "tools": modes("tools"),
        "mcp_servers": modes("mcp_servers"),
    }


def tool_approval_config_errors(raw: Any, *, label: str = "permissions.tool_approval") -> list[str]:
    if not isinstance(raw, dict):
        return [f"{label} 必须是对象。"]
    errors: list[str] = []
    allowed_keys = {"default_external", "tools", "mcp_servers"}
    for key in sorted(raw):
        if key not in allowed_keys:
            errors.append(f"{label}.{key} 不支持。")
    if "default_external" in raw:
        _validate_mode(raw["default_external"], f"{label}.default_external", errors)
    for key in ("tools", "mcp_servers"):
        if key not in raw:
            continue
        configured = raw[key]
        if not isinstance(configured, dict):
            errors.append(f"{label}.{key} 必须是对象。")
            continue
        for name, mode in sorted(configured.items(), key=lambda item: str(item[0])):
            _validate_mode(mode, f"{label}.{key}.{name}", errors)
    return errors


def _validate_mode(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, str) or value.strip().lower() not in TOOL_APPROVAL_MODES:
        errors.append(
            f"{label} 必须是 auto/cached/prompt/deny，可选: "
            + ", ".join(sorted(TOOL_APPROVAL_MODES))
        )
