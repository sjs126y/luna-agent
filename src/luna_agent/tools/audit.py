"""Audit log — records all file I/O and shell executions for traceability."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_AUDIT_PATH: Path = Path("./data/audit.log")
_AUDIT_LOCK = None  # lazy init


def set_audit_path(path: Path) -> None:
    global _AUDIT_PATH
    _AUDIT_PATH = path


def _get_lock():
    global _AUDIT_LOCK
    if _AUDIT_LOCK is None:
        import threading
        _AUDIT_LOCK = threading.Lock()
    return _AUDIT_LOCK


def _write_entry(entry: dict[str, Any]) -> None:
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _get_lock():
        with open(_AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(line)


def audit_log(tool: str, detail: str, result_snippet: str, success: bool) -> None:
    """Append one JSON line to the audit log. Non-blocking — errors are suppressed.

    All fields are redacted to mask API keys and tokens before writing.
    """
    try:
        from luna_agent.tools.redact import redact
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tool": tool,
            "detail": redact(detail[:500]),
            "result": redact(result_snippet[:200]),
            "success": success,
        }
        _write_entry(entry)
    except Exception:
        pass  # audit failure never blocks operations


def audit_tool_decision(decision) -> None:
    """Append one structured tool-decision audit record."""
    try:
        from luna_agent.tools.redact import redact

        data = decision.as_dict() if hasattr(decision, "as_dict") else dict(decision)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "event": "tool_decision",
            "tool": redact(str(data.get("tool_name", ""))[:200]),
            "tool_use_id": redact(str(data.get("tool_use_id", ""))[:200]),
            "allowed": bool(data.get("allowed", False)),
            "stage": str(data.get("stage", "")),
            "status": str(data.get("status", "")),
            "permission_category": str(data.get("permission_category", "")),
            "execution_mode": str(data.get("execution_mode", "")),
            "permission_decision": str(data.get("permission_decision", "")),
            "reason_code": str(data.get("reason_code", "")),
            "required_allow": str(data.get("required_allow", "")),
            "grant_matched": str(data.get("grant_matched", "")),
            "message": redact(str(data.get("decision_message", data.get("message", "")))[:500]),
        }
        _write_entry(entry)
    except Exception:
        pass


def audit_tool_result(result, *, decision=None) -> None:
    """Append one structured tool-result audit record."""
    try:
        from luna_agent.tools.redact import redact

        result_data = result.as_dict() if hasattr(result, "as_dict") else dict(result)
        decision_data = (
            decision.as_dict()
            if hasattr(decision, "as_dict")
            else dict(decision or {})
        )
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "event": "tool_result",
            "tool": redact(str(result_data.get("tool_name", ""))[:200]),
            "tool_use_id": redact(str(result_data.get("tool_use_id", ""))[:200]),
            "status": str(result_data.get("status", "")),
            "category": str(result_data.get("category", "")),
            "permission_category": str(decision_data.get("permission_category", "")),
            "execution_mode": str(decision_data.get("execution_mode", "")),
            "permission_decision": str(decision_data.get("permission_decision", "")),
            "reason_code": str(decision_data.get("reason_code", "")),
            "required_allow": str(decision_data.get("required_allow", "")),
            "grant_matched": str(decision_data.get("grant_matched", "")),
            "duration": float(result_data.get("duration", 0.0) or 0.0),
            "attempts": int(result_data.get("attempts", 0) or 0),
            "input_summary": redact(str(result_data.get("input_summary", ""))[:500]),
            "output_summary": redact(str(result_data.get("output_summary", ""))[:500]),
            "error": redact(str(result_data.get("error", ""))[:500]),
        }
        _write_entry(entry)
    except Exception:
        pass
