"""Async-local context available to tool handlers without changing schemas."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

_CURRENT_AGENT: ContextVar[Any | None] = ContextVar("personal_agent_current_tool_agent", default=None)


def set_current_tool_agent(agent: Any | None) -> Token:
    return _CURRENT_AGENT.set(agent)


def reset_current_tool_agent(token: Token) -> None:
    _CURRENT_AGENT.reset(token)


def current_tool_agent() -> Any | None:
    return _CURRENT_AGENT.get()
