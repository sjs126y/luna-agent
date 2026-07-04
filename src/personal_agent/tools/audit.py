"""Audit log — records all file I/O and shell executions for traceability."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

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


def audit_log(tool: str, detail: str, result_snippet: str, success: bool) -> None:
    """Append one JSON line to the audit log. Non-blocking — errors are suppressed.

    All fields are redacted to mask API keys and tokens before writing.
    """
    try:
        from personal_agent.tools.redact import redact
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tool": tool,
            "detail": redact(detail[:500]),
            "result": redact(result_snippet[:200]),
            "success": success,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _get_lock():
            with open(_AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass  # audit failure never blocks operations


def audit_tool_decision(decision) -> None:
    """Append one structured tool-decision audit record."""
    try:
        from personal_agent.tools.redact import redact

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
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _get_lock():
            with open(_AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass
