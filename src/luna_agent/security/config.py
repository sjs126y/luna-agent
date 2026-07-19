"""Validation and normalization for tool approval settings."""

from __future__ import annotations

from typing import Any

TOOL_APPROVAL_MODES = {"auto", "cached", "prompt", "deny"}


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
