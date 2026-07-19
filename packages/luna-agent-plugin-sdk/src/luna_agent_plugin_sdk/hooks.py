"""Stable hook wire models and event-specific outcomes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class HookEvent(str, Enum):
    GATEWAY_START = "GatewayStart"
    GATEWAY_STOP = "GatewayStop"
    PLATFORM_CONNECTED = "PlatformConnected"
    PLATFORM_DISCONNECTED = "PlatformDisconnected"
    GATEWAY_MESSAGE_RECEIVED = "GatewayMessageReceived"
    PRE_DELIVERY = "PreDelivery"
    POST_DELIVERY = "PostDelivery"
    SESSION_START = "SessionStart"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    STOP = "Stop"
    PRE_TOOL_USE = "PreToolUse"
    PERMISSION_REQUEST = "PermissionRequest"
    POST_TOOL_USE = "PostToolUse"


class HookScope(str, Enum):
    RUNTIME = "runtime"
    SESSION = "session"
    TURN = "turn"


class HookSource(str, Enum):
    CORE = "core"
    PLUGIN = "plugin"


class PermissionDecision(str, Enum):
    ABSTAIN = "abstain"
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class HookSourceContext:
    platform: str = ""
    user_id: str = ""
    chat_id: str = ""


@dataclass(frozen=True)
class HookEnvelope:
    event_name: HookEvent
    scope: HookScope
    session_key: str = ""
    turn_id: str = ""
    agent_id: str = "main"
    cwd: str = ""
    mode: str = ""
    triggered_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    source: HookSourceContext | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def with_payload(self, **changes: Any) -> "HookEnvelope":
        return replace(self, payload={**dict(self.payload), **changes})

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


@dataclass(frozen=True)
class ContextHookOutcome:
    additional_context: str = ""
    stop: bool = False
    reason: str = ""


@dataclass(frozen=True)
class GatewayMessageOutcome:
    blocked: bool = False
    reason: str = ""
    text: str | None = None
    attachments: tuple[Any, ...] | None = None
    metadata: Mapping[str, Any] | None = None

    @classmethod
    def block(cls, reason: str) -> "GatewayMessageOutcome":
        return cls(blocked=True, reason=str(reason or "message blocked by hook"))

    @classmethod
    def replace_message(
        cls,
        *,
        text: str | None = None,
        attachments: tuple[Any, ...] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "GatewayMessageOutcome":
        return cls(text=text, attachments=attachments, metadata=metadata)


@dataclass(frozen=True)
class PreDeliveryOutcome:
    suppressed: bool = False
    reason: str = ""
    text: str | None = None
    removed_artifact_ids: tuple[str, ...] = ()

    @classmethod
    def suppress(cls, reason: str) -> "PreDeliveryOutcome":
        return cls(suppressed=True, reason=str(reason or "delivery suppressed by hook"))

    @classmethod
    def replace_text(cls, text: str) -> "PreDeliveryOutcome":
        return cls(text=str(text))

    @classmethod
    def remove_artifacts(cls, *artifact_ids: str, reason: str = "") -> "PreDeliveryOutcome":
        return cls(
            reason=str(reason or ""),
            removed_artifact_ids=tuple(
                dict.fromkeys(
                    str(value or "").strip()
                    for value in artifact_ids
                    if str(value or "").strip()
                )
            ),
        )


@dataclass(frozen=True)
class PreToolUseOutcome:
    blocked: bool = False
    reason: str = ""
    additional_context: str = ""
    updated_input: Mapping[str, Any] | None = None

    @classmethod
    def block(cls, reason: str) -> "PreToolUseOutcome":
        return cls(blocked=True, reason=str(reason or "tool blocked by hook"))


@dataclass(frozen=True)
class PermissionRequestOutcome:
    decision: PermissionDecision = PermissionDecision.ABSTAIN
    reason: str = ""


@dataclass(frozen=True)
class PostToolUseOutcome:
    blocked: bool = False
    reason: str = ""
    additional_context: str = ""


@dataclass(frozen=True)
class StopOutcome:
    continue_turn: bool = False
    reason: str = ""
    continuation_prompt: str = ""


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return value
