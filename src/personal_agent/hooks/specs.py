"""Event specifications for hook execution and validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from personal_agent.hooks.models import (
    ContextHookOutcome,
    GatewayBeforeSendOutcome,
    GatewayMessageOutcome,
    HookEvent,
    PostToolUseOutcome,
    PreToolUseOutcome,
    PermissionRequestOutcome,
    StopOutcome,
)

HookExecution = Literal["pipeline", "policy", "context", "observer"]
HookFailure = Literal["continue", "block"]


@dataclass(frozen=True)
class HookSpec:
    execution: HookExecution
    outcome_type: type | None
    default_timeout_seconds: float
    failure: HookFailure = "continue"


HOOK_SPECS: dict[HookEvent, HookSpec] = {
    HookEvent.GATEWAY_START: HookSpec("observer", None, 10.0),
    HookEvent.GATEWAY_STOP: HookSpec("observer", None, 10.0),
    HookEvent.PLATFORM_CONNECTED: HookSpec("observer", None, 10.0),
    HookEvent.PLATFORM_DISCONNECTED: HookSpec("observer", None, 10.0),
    HookEvent.GATEWAY_MESSAGE_RECEIVED: HookSpec("pipeline", GatewayMessageOutcome, 3.0),
    HookEvent.GATEWAY_BEFORE_SEND: HookSpec("pipeline", GatewayBeforeSendOutcome, 3.0),
    HookEvent.GATEWAY_AFTER_SEND: HookSpec("observer", None, 3.0),
    HookEvent.SESSION_START: HookSpec("context", ContextHookOutcome, 10.0),
    HookEvent.USER_PROMPT_SUBMIT: HookSpec("context", ContextHookOutcome, 3.0),
    HookEvent.PRE_COMPACT: HookSpec("context", ContextHookOutcome, 10.0),
    HookEvent.POST_COMPACT: HookSpec("observer", None, 10.0),
    HookEvent.STOP: HookSpec("policy", StopOutcome, 10.0),
    HookEvent.PRE_TOOL_USE: HookSpec("policy", PreToolUseOutcome, 3.0, failure="block"),
    HookEvent.PERMISSION_REQUEST: HookSpec("policy", PermissionRequestOutcome, 2.0),
    HookEvent.POST_TOOL_USE: HookSpec("policy", PostToolUseOutcome, 3.0),
}


def hook_spec(event: HookEvent) -> HookSpec:
    return HOOK_SPECS[event]


def matcher_value(event: HookEvent, envelope) -> str | None:
    payload = envelope.payload
    if event in {
        HookEvent.GATEWAY_MESSAGE_RECEIVED,
        HookEvent.GATEWAY_BEFORE_SEND,
        HookEvent.GATEWAY_AFTER_SEND,
        HookEvent.PLATFORM_CONNECTED,
        HookEvent.PLATFORM_DISCONNECTED,
    }:
        return envelope.source.platform if envelope.source else str(payload.get("platform") or "")
    if event == HookEvent.SESSION_START:
        return str(payload.get("source") or "")
    if event == HookEvent.USER_PROMPT_SUBMIT:
        return envelope.source.platform if envelope.source else ""
    if event in {
        HookEvent.PRE_TOOL_USE,
        HookEvent.PERMISSION_REQUEST,
        HookEvent.POST_TOOL_USE,
    }:
        return str(payload.get("tool_name") or "")
    if event in {HookEvent.PRE_COMPACT, HookEvent.POST_COMPACT}:
        return str(payload.get("trigger") or "")
    return None
