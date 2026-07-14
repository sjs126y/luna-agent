"""Hook registration, dispatch, aggregation, timeout, and diagnostics."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from personal_agent.hooks.models import (
    ContextHookOutcome,
    GatewayBeforeSendOutcome,
    GatewayMessageOutcome,
    HookEnvelope,
    HookEvent,
    HookSource,
    PermissionDecision,
    PermissionRequestOutcome,
    PostToolUseOutcome,
    PreToolUseOutcome,
    StopOutcome,
)
from personal_agent.hooks.specs import hook_spec, matcher_value

logger = logging.getLogger(__name__)

HookCallback = Callable[[HookEnvelope], Any]


@dataclass
class HookStats:
    execution_count: int = 0
    blocked_count: int = 0
    timeout_count: int = 0
    failure_count: int = 0
    last_duration_ms: float = 0.0
    last_error: str = ""


@dataclass(frozen=True)
class HookRegistration:
    hook_id: str
    owner: str
    source: HookSource
    event: HookEvent
    name: str
    callback: HookCallback
    matcher: str
    pattern: re.Pattern[str] | None
    priority: int
    timeout_seconds: float
    order: int


@dataclass
class _Execution:
    registration: HookRegistration
    outcome: Any = None
    error: str = ""
    timed_out: bool = False


class HookManager:
    def __init__(self) -> None:
        self._registrations: dict[HookEvent, tuple[HookRegistration, ...]] = {}
        self._stats: dict[str, HookStats] = {}
        self._next_order = 0

    def register(
        self,
        *,
        owner: str,
        event: HookEvent | str,
        callback: HookCallback,
        name: str = "",
        matcher: str = "*",
        priority: int = 100,
        timeout_seconds: float | None = None,
        source: HookSource | str = HookSource.PLUGIN,
    ) -> HookRegistration:
        normalized_event = event if isinstance(event, HookEvent) else HookEvent(str(event))
        normalized_source = source if isinstance(source, HookSource) else HookSource(str(source))
        normalized_owner = str(owner or "").strip()
        if not normalized_owner:
            raise ValueError("Hook owner is required")
        if not callable(callback):
            raise TypeError("Hook callback must be callable")
        normalized_name = str(name or getattr(callback, "__name__", "hook") or "hook").strip()
        normalized_matcher = str(matcher or "*").strip() or "*"
        pattern = None
        if normalized_matcher != "*":
            try:
                pattern = re.compile(normalized_matcher)
            except re.error as exc:
                raise ValueError(f"Invalid hook matcher '{normalized_matcher}': {exc}") from exc
        timeout = hook_spec(normalized_event).default_timeout_seconds
        if timeout_seconds is not None:
            timeout = float(timeout_seconds)
        if timeout <= 0 or timeout > 60:
            raise ValueError("Hook timeout must be greater than 0 and at most 60 seconds")
        hook_id = f"{normalized_owner}:{normalized_event.value}:{normalized_name}"
        if hook_id in self._stats:
            raise ValueError(f"Hook already registered: {hook_id}")
        registration = HookRegistration(
            hook_id=hook_id,
            owner=normalized_owner,
            source=normalized_source,
            event=normalized_event,
            name=normalized_name,
            callback=callback,
            matcher=normalized_matcher,
            pattern=pattern,
            priority=int(priority),
            timeout_seconds=timeout,
            order=self._next_order,
        )
        self._next_order += 1
        items = [*self._registrations.get(normalized_event, ()), registration]
        items.sort(key=lambda item: (item.priority, item.order))
        self._registrations[normalized_event] = tuple(items)
        self._stats[hook_id] = HookStats()
        return registration

    def unregister_owner(self, owner: str) -> list[str]:
        removed: list[str] = []
        for event, registrations in list(self._registrations.items()):
            kept = []
            for registration in registrations:
                if registration.owner == owner:
                    removed.append(registration.hook_id)
                    self._stats.pop(registration.hook_id, None)
                else:
                    kept.append(registration)
            if kept:
                self._registrations[event] = tuple(kept)
            else:
                self._registrations.pop(event, None)
        return removed

    def registrations(self, event: HookEvent | None = None) -> list[HookRegistration]:
        if event is not None:
            return list(self._registrations.get(event, ()))
        return [
            registration
            for hook_event in HookEvent
            for registration in self._registrations.get(hook_event, ())
        ]

    async def dispatch(self, envelope: HookEnvelope) -> Any:
        event = envelope.event_name
        registrations = tuple(
            registration
            for registration in self._registrations.get(event, ())
            if self._matches(registration, envelope)
        )
        if not registrations:
            return self._empty_outcome(event)
        spec = hook_spec(event)
        if spec.execution == "pipeline":
            return await self._dispatch_pipeline(envelope, registrations)
        executions = await asyncio.gather(
            *(self._execute(registration, envelope) for registration in registrations)
        )
        if spec.execution == "observer":
            return None
        if spec.execution == "context":
            return self._aggregate_context(executions)
        return self._aggregate_policy(event, executions)

    def health_snapshot(self) -> dict[str, Any]:
        items = []
        for registration in self.registrations():
            stats = self._stats[registration.hook_id]
            items.append({
                "hook_id": registration.hook_id,
                "owner": registration.owner,
                "source": registration.source.value,
                "event_name": registration.event.value,
                "name": registration.name,
                "matcher": registration.matcher,
                "priority": registration.priority,
                "timeout_seconds": registration.timeout_seconds,
                "execution_count": stats.execution_count,
                "blocked_count": stats.blocked_count,
                "timeout_count": stats.timeout_count,
                "failure_count": stats.failure_count,
                "last_duration_ms": round(stats.last_duration_ms, 3),
                "last_error": stats.last_error,
            })
        return {
            "registered": len(items),
            "owners": len({item["owner"] for item in items}),
            "events": sorted({item["event_name"] for item in items}),
            "items": items,
        }

    def _matches(self, registration: HookRegistration, envelope: HookEnvelope) -> bool:
        if registration.pattern is None:
            return True
        value = matcher_value(registration.event, envelope)
        return value is not None and registration.pattern.fullmatch(value) is not None

    async def _execute(
        self,
        registration: HookRegistration,
        envelope: HookEnvelope,
    ) -> _Execution:
        stats = self._stats[registration.hook_id]
        stats.execution_count += 1
        started = time.monotonic()
        try:
            value = registration.callback(envelope)
            if inspect.isawaitable(value):
                value = await asyncio.wait_for(value, timeout=registration.timeout_seconds)
            execution = _Execution(registration=registration, outcome=value)
        except asyncio.TimeoutError:
            execution = _Execution(
                registration=registration,
                error=f"hook timed out after {registration.timeout_seconds:g}s",
                timed_out=True,
            )
        except Exception as exc:
            execution = _Execution(
                registration=registration,
                error=f"{type(exc).__name__}: {exc}",
            )
        stats.last_duration_ms = (time.monotonic() - started) * 1000
        if execution.error:
            stats.failure_count += 1
            stats.timeout_count += int(execution.timed_out)
            stats.last_error = execution.error
            logger.warning(
                "Hook failed: id=%s error=%s",
                registration.hook_id,
                execution.error,
            )
            return execution
        stats.last_error = ""
        expected = hook_spec(registration.event).outcome_type
        if execution.outcome is not None and expected is not None and not isinstance(execution.outcome, expected):
            execution.error = (
                f"invalid outcome type: expected {expected.__name__}, "
                f"got {type(execution.outcome).__name__}"
            )
            stats.failure_count += 1
            stats.last_error = execution.error
        return execution

    async def _dispatch_pipeline(
        self,
        envelope: HookEnvelope,
        registrations: tuple[HookRegistration, ...],
    ) -> Any:
        current = envelope
        if envelope.event_name == HookEvent.GATEWAY_MESSAGE_RECEIVED:
            accumulated: dict[str, Any] = {}
            for registration in registrations:
                execution = await self._execute(registration, current)
                outcome = execution.outcome if not execution.error else None
                if not isinstance(outcome, GatewayMessageOutcome):
                    continue
                if outcome.blocked:
                    self._mark_blocked(registration)
                    return outcome
                changes = {}
                if outcome.text is not None:
                    changes["text"] = outcome.text
                if outcome.attachments is not None:
                    changes["attachments"] = outcome.attachments
                if outcome.metadata is not None:
                    changes["metadata"] = dict(outcome.metadata)
                if changes:
                    current = current.with_payload(**changes)
                    accumulated.update(changes)
            return GatewayMessageOutcome(**accumulated)
        final_send = GatewayBeforeSendOutcome()
        for registration in registrations:
            execution = await self._execute(registration, current)
            outcome = execution.outcome if not execution.error else None
            if not isinstance(outcome, GatewayBeforeSendOutcome):
                continue
            if outcome.suppressed:
                self._mark_blocked(registration)
                return outcome
            if outcome.text is not None:
                current = current.with_payload(text=outcome.text)
                final_send = GatewayBeforeSendOutcome(text=outcome.text)
        return final_send

    def _aggregate_context(self, executions: list[_Execution]) -> ContextHookOutcome:
        contexts: list[str] = []
        stop = False
        reason = ""
        for execution in executions:
            if execution.error or execution.outcome is None:
                continue
            outcome = execution.outcome
            if not isinstance(outcome, ContextHookOutcome):
                continue
            if outcome.additional_context.strip():
                contexts.append(outcome.additional_context.strip())
            if outcome.stop and not stop:
                stop = True
                reason = outcome.reason
                self._mark_blocked(execution.registration)
        return ContextHookOutcome(
            additional_context="\n\n".join(contexts),
            stop=stop,
            reason=reason,
        )

    def _aggregate_policy(self, event: HookEvent, executions: list[_Execution]) -> Any:
        if event == HookEvent.PRE_TOOL_USE:
            return self._aggregate_pre_tool(executions)
        if event == HookEvent.PERMISSION_REQUEST:
            return self._aggregate_permission(executions)
        if event == HookEvent.POST_TOOL_USE:
            return self._aggregate_post_tool(executions)
        if event == HookEvent.STOP:
            return self._aggregate_stop(executions)
        return None

    def _aggregate_pre_tool(self, executions: list[_Execution]) -> PreToolUseOutcome:
        contexts: list[str] = []
        rewrite = None
        for execution in executions:
            if execution.error:
                if hook_spec(HookEvent.PRE_TOOL_USE).failure == "block":
                    self._mark_blocked(execution.registration)
                    return PreToolUseOutcome.block(execution.error)
                continue
            outcome = execution.outcome
            if outcome is None:
                continue
            if outcome.additional_context.strip():
                contexts.append(outcome.additional_context.strip())
            if outcome.blocked:
                self._mark_blocked(execution.registration)
                return PreToolUseOutcome(
                    blocked=True,
                    reason=outcome.reason,
                    additional_context="\n\n".join(contexts),
                )
            if rewrite is None and outcome.updated_input is not None:
                rewrite = dict(outcome.updated_input)
            elif rewrite is not None and outcome.updated_input is not None:
                logger.warning(
                    "Ignored lower-priority hook input rewrite: %s",
                    execution.registration.hook_id,
                )
        return PreToolUseOutcome(
            additional_context="\n\n".join(contexts),
            updated_input=rewrite,
        )

    def _aggregate_permission(self, executions: list[_Execution]) -> PermissionRequestOutcome:
        allow: PermissionRequestOutcome | None = None
        for execution in executions:
            if execution.error or execution.outcome is None:
                continue
            outcome = execution.outcome
            if outcome.decision == PermissionDecision.DENY:
                self._mark_blocked(execution.registration)
                return outcome
            if allow is None and outcome.decision == PermissionDecision.ALLOW:
                allow = outcome
        return allow or PermissionRequestOutcome()

    def _aggregate_post_tool(self, executions: list[_Execution]) -> PostToolUseOutcome:
        contexts: list[str] = []
        blocked = False
        reason = ""
        for execution in executions:
            if execution.error or execution.outcome is None:
                continue
            outcome = execution.outcome
            if outcome.additional_context.strip():
                contexts.append(outcome.additional_context.strip())
            if outcome.blocked and not blocked:
                blocked = True
                reason = outcome.reason
                self._mark_blocked(execution.registration)
        return PostToolUseOutcome(
            blocked=blocked,
            reason=reason,
            additional_context="\n\n".join(contexts),
        )

    def _aggregate_stop(self, executions: list[_Execution]) -> StopOutcome:
        prompts: list[str] = []
        reason = ""
        for execution in executions:
            if execution.error or execution.outcome is None:
                continue
            outcome = execution.outcome
            if outcome.continue_turn:
                self._mark_blocked(execution.registration)
                if not reason:
                    reason = outcome.reason
                if outcome.continuation_prompt.strip():
                    prompts.append(outcome.continuation_prompt.strip())
        return StopOutcome(
            continue_turn=bool(prompts or reason),
            reason=reason,
            continuation_prompt="\n\n".join(prompts),
        )

    def _mark_blocked(self, registration: HookRegistration) -> None:
        self._stats[registration.hook_id].blocked_count += 1

    @staticmethod
    def _empty_outcome(event: HookEvent) -> Any:
        expected = hook_spec(event).outcome_type
        return expected() if expected is not None else None
