"""Trace ID via contextvars — zero-argument plumbing through deep call chains.

Usage:
  from personal_agent.trace import trace_id, set_trace

  # At entry point (Gateway, CLI):
  token = set_trace("wechat:user123:1719158400")
  try:
      ... entire request handling ...
  finally:
      trace_id.reset(token)

  # Anywhere deep in the call stack (no function signature changes):
  tid = trace_id.get()  # "wechat:user123:1719158400" or ""

The logging filter auto-injects it into every log line — no code changes needed.
"""

from __future__ import annotations

import contextvars
import logging
import time

trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default=""
)


def set_trace(prefix: str) -> contextvars.Token:
    """Set trace_id = prefix + timestamp. Returns token for reset()."""
    tid = f"{prefix}_{int(time.time() * 1000) % 100000:05d}"
    return trace_id.set(tid)


class TraceFilter(logging.Filter):
    """Inject trace_id into every log record automatically."""

    def filter(self, record: logging.LogRecord) -> bool:
        tid = trace_id.get()
        record.trace_id = tid[:16] if tid else "-"  # keep it short
        return True
