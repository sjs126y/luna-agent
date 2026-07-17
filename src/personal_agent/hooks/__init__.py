"""Typed lifecycle hooks shared by Gateway, conversations, and tools."""

from personal_agent.hooks.manager import HookManager
from personal_agent.hooks.models import (
    ContextHookOutcome,
    GatewayMessageOutcome,
    HookEnvelope,
    HookEvent,
    HookScope,
    HookSource,
    HookSourceContext,
    PermissionDecision,
    PermissionRequestOutcome,
    PostToolUseOutcome,
    PreDeliveryOutcome,
    PreToolUseOutcome,
    StopOutcome,
)

__all__ = [
    "ContextHookOutcome",
    "GatewayMessageOutcome",
    "HookEnvelope",
    "HookEvent",
    "HookManager",
    "HookScope",
    "HookSource",
    "HookSourceContext",
    "PermissionDecision",
    "PermissionRequestOutcome",
    "PostToolUseOutcome",
    "PreDeliveryOutcome",
    "PreToolUseOutcome",
    "StopOutcome",
]
