"""Trace ID via contextvars — zero-argument plumbing through deep call chains.

Usage:
  from luna_agent.trace import trace_id, set_trace

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
from contextlib import contextmanager
from typing import Iterator

trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default=""
)
session_key: contextvars.ContextVar[str] = contextvars.ContextVar("session_key", default="")
request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
turn_id: contextvars.ContextVar[str] = contextvars.ContextVar("turn_id", default="")
operation_id: contextvars.ContextVar[str] = contextvars.ContextVar("operation_id", default="")


def set_trace(prefix: str) -> contextvars.Token:
    """Set trace_id = prefix + timestamp. Returns token for reset()."""
    tid = f"{prefix}_{int(time.time() * 1000) % 100000:05d}"
    return trace_id.set(tid)


def current_context() -> dict[str, str]:
    return {
        "trace_id": trace_id.get(),
        "session_key": session_key.get(),
        "request_id": request_id.get(),
        "turn_id": turn_id.get(),
        "operation_id": operation_id.get(),
    }


@contextmanager
def context(**values: str) -> Iterator[None]:
    vars_by_name = {
        "trace_id": trace_id,
        "session_key": session_key,
        "request_id": request_id,
        "turn_id": turn_id,
        "operation_id": operation_id,
    }
    tokens = [vars_by_name[name].set(value) for name, value in values.items() if name in vars_by_name and value]
    try:
        yield
    finally:
        for var, token in zip((vars_by_name[name] for name, value in values.items() if name in vars_by_name and value), tokens):
            var.reset(token)


class TraceFilter(logging.Filter):
    """Inject trace_id into every log record automatically."""

    def filter(self, record: logging.LogRecord) -> bool:
        tid = trace_id.get()
        record.trace_id = tid[:16] if tid else "-"  # keep it short
        record.session_key = session_key.get() or "-"
        record.request_id = request_id.get() or "-"
        record.turn_id = turn_id.get() or "-"
        record.operation_id = operation_id.get() or "-"
        return True
