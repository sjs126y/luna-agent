"""Async-local context available to tool handlers without changing schemas."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

_CURRENT_AGENT: ContextVar[Any | None] = ContextVar("personal_agent_current_tool_agent", default=None)
_CURRENT_CONFIRM: ContextVar[Any | None] = ContextVar("personal_agent_current_tool_confirm", default=None)
_CURRENT_HOOKS: ContextVar[Any | None] = ContextVar("personal_agent_current_tool_hooks", default=None)
_CURRENT_EVENT_SINK: ContextVar[Any | None] = ContextVar("personal_agent_current_tool_event_sink", default=None)


def set_current_tool_agent(agent: Any | None) -> Token:
    return _CURRENT_AGENT.set(agent)


def reset_current_tool_agent(token: Token) -> None:
    _CURRENT_AGENT.reset(token)


def current_tool_agent() -> Any | None:
    return _CURRENT_AGENT.get()


def set_current_tool_confirm(confirm: Any | None) -> Token:
    return _CURRENT_CONFIRM.set(confirm)


def reset_current_tool_confirm(token: Token) -> None:
    _CURRENT_CONFIRM.reset(token)


def current_tool_confirm() -> Any | None:
    return _CURRENT_CONFIRM.get()


def set_current_tool_hooks(hooks: Any | None) -> Token:
    return _CURRENT_HOOKS.set(hooks)


def reset_current_tool_hooks(token: Token) -> None:
    _CURRENT_HOOKS.reset(token)


def current_tool_hooks() -> Any | None:
    return _CURRENT_HOOKS.get()


def set_current_tool_event_sink(event_sink: Any | None) -> Token:
    return _CURRENT_EVENT_SINK.set(event_sink)


def reset_current_tool_event_sink(token: Token) -> None:
    _CURRENT_EVENT_SINK.reset(token)


def current_tool_event_sink() -> Any | None:
    return _CURRENT_EVENT_SINK.get()
