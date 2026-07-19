"""Shared conversation runtime for CLI and Gateway entrypoints."""

from __future__ import annotations

import inspect
import hashlib
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
import time
from typing import Any
import uuid

from luna_agent.attachments import AttachmentStore
from luna_agent.conversation.events import ConversationEvent, EventRecorder, emit_event
from luna_agent.conversation.input import ConversationInput, ensure_conversation_input
from luna_agent.conversation.steer import SteerManager, SteerSignal
from luna_agent.conversation.transcript import build_stopped_turn_transcript
from luna_agent.models.messages import MessagePart, OutboundMessage, SessionSource
from luna_agent.multimodal.processor import MultiAttachmentProcessor

TURN_REPORT_HISTORY_LIMIT = 50
EMPTY_FINAL_RESPONSE_MESSAGE = "抱歉，模型没有返回可发送内容，请重试或让我用更短的格式回答。"


class _HookTurnStopped(Exception):
    """Stop a turn before the agent loop without treating it as a runtime failure."""


@dataclass
class ConversationTurnResult:
    final_response: str
    messages: list[dict]
    completed: bool
    context_overflow: bool
    was_compressed: bool
    should_review_memory: bool
    raw: dict[str, Any]
    status: str = "completed"
    error: str = ""
    events: list[ConversationEvent] = field(default_factory=list)
    turn_report: dict[str, Any] = field(default_factory=dict)
    outbound_message: OutboundMessage = field(default_factory=OutboundMessage)


class ConversationService:
    def __init__(
        self,
        *,
        settings,
        plugin_manager,
        hook_manager=None,
        session_store,
        compression_chain,
        memory_manager,
        memory_review_service=None,
        artifact_store=None,
        system_prompt_template: str = "",
        agent_cache: dict[str, object] | OrderedDict[str, object] | None = None,
        agent_cache_max: int | None = None,
    ) -> None:
        self.settings = settings
        self.plugin_manager = plugin_manager
        self.hook_manager = hook_manager or getattr(plugin_manager, "hook_manager", None)
        self.session_store = session_store
        self.compression_chain = compression_chain
        self.memory_manager = memory_manager
        self.memory_review_service = memory_review_service
        self.artifact_store = artifact_store
        self.system_prompt_template = system_prompt_template
        self.agent_cache_max = agent_cache_max
        self.agent_cache: OrderedDict[str, object] = (
            agent_cache
            if isinstance(agent_cache, OrderedDict)
            else OrderedDict(agent_cache or {})
        )
        self.turn_reports: deque[dict[str, Any]] = deque(maxlen=TURN_REPORT_HISTORY_LIMIT)
        self._recent_tool_runs: deque[dict[str, Any]] = deque(maxlen=TURN_REPORT_HISTORY_LIMIT)
        self._persisted_turn_report_count = 0
        self._last_persisted_turn_report: dict[str, Any] = {}
        self._last_persisted_turn_report_error = ""
        self._hook_started_session_ids: set[str] = set()
        self._pending_session_start_sources: dict[str, str] = {}
        self.attachment_store = AttachmentStore(Path(settings.agent_data_dir) / "attachments")
        self.multimodal_processor = MultiAttachmentProcessor(
            settings=settings,
            attachment_store=self.attachment_store,
        )
        self.steer_manager = SteerManager()
        from luna_agent.security.session import SecurityStateStore

        self.security_states = SecurityStateStore(settings)
        from luna_agent.conversation.query import ConversationQueryService

        self.queries = ConversationQueryService(self)

    async def run_turn(self, session_key: str, source, text: str) -> ConversationTurnResult:
        return await self.run_turn_events(session_key, source, text)

    async def run_turn_input(
        self,
        session_key: str,
        user_input: ConversationInput,
        *,
        event_sink=None,
        confirm=None,
    ) -> ConversationTurnResult:
        return await self.run_turn_input_events(
            session_key,
            user_input,
            event_sink=event_sink,
            confirm=confirm,
        )

    async def run_turn_events(
        self,
        session_key: str,
        source,
        text: str,
        *,
        event_sink=None,
        confirm=None,
    ) -> ConversationTurnResult:
        user_input = ensure_conversation_input(text, source=source)
        return await self.run_turn_input_events(
            session_key,
            user_input,
            event_sink=event_sink,
            confirm=confirm,
        )

    async def run_turn_input_events(
        self,
        session_key: str,
        user_input: ConversationInput,
        *,
        event_sink=None,
        confirm=None,
        turn_id: str = "",
        steer=None,
        policy_snapshot=None,
        capability_view=None,
    ) -> ConversationTurnResult:
        recorder = EventRecorder(event_sink)
        if self.plugin_manager is not None:
            await self.plugin_manager.invoke_hook("on_session_selected", session_key=session_key)

        source = user_input.source or SessionSource(platform="unknown", user_id="unknown")
        previous_session_id = self.session_store.resolve_session_id(session_key)
        session = await self.session_store.get_or_create(session_key, source)
        current_id = self.resolve_session_id(session.session_id)
        history = await self.session_store.load_history(current_id)
        previous_count = len(history)
        turn_id = str(turn_id or f"{uuid.uuid4().hex[:8]}")
        turn_steer = steer or self.steer_manager
        owns_turn = steer is None
        if owns_turn:
            turn_steer.begin_turn(session_key, turn_id)

        from luna_agent.agent.context import build_turn_context
        from luna_agent.agent.loop import run_conversation

        ctx = None
        try:
            hook_contexts = await self._conversation_hook_contexts(
                session_key=session_key,
                session_id=current_id,
                turn_id=turn_id,
                source=source,
                user_input=user_input,
                previous_session_id=previous_session_id,
            )
            agent = await self.get_or_create_agent(
                session_key,
                capability_view=capability_view,
            )
            if policy_snapshot is not None:
                agent._security_context = policy_snapshot.security
            agent._hook_source = source
            resolved_input = await self.multimodal_processor.resolve(
                user_input,
                provider=getattr(agent, "_provider", None),
            )
            ctx_input = resolved_input if user_input.attachments else resolved_input.text
            if _accepts_turn_id(build_turn_context):
                ctx = await build_turn_context(agent, ctx_input, history, turn_id=turn_id)
            else:
                ctx = await build_turn_context(agent, ctx_input, history)
                if not str(getattr(ctx, "turn_id", "") or ""):
                    setattr(ctx, "turn_id", turn_id)
            context_items = getattr(ctx, "hook_contexts", None)
            if context_items is None:
                context_items = []
                setattr(ctx, "hook_contexts", context_items)
            context_items.extend(hook_contexts)
            kwargs = {}
            if _accepts_event_sink(run_conversation):
                kwargs["event_sink"] = recorder
            if _accepts_confirm(run_conversation):
                kwargs["confirm"] = confirm
            if _accepts_steer(run_conversation):
                kwargs["steer"] = turn_steer
                kwargs["session_key"] = session_key
            result = await run_conversation(agent, ctx, **kwargs)
        except _HookTurnStopped as exc:
            final = str(exc) or "本轮已被钩子停止。"
            await emit_event(
                recorder,
                "stop",
                final,
                reason="hook",
                stopped_tools=0,
                stopped_agents=0,
            )
            result = {
                "final_response": final,
                "messages": _minimal_turn_messages(user_input.text, final),
                "completed": False,
                "status": "stopped",
                "error": "",
            }
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            final = f"抱歉，本轮处理出错了：{exc}"
            await emit_event(
                recorder,
                "error",
                "本轮处理失败",
                error=error,
                category="runtime",
                recoverable=False,
                detail_id=_event_detail_id("runtime", error),
            )
            result = {
                "final_response": final,
                "messages": _minimal_turn_messages(user_input.text, final),
                "completed": False,
                "status": "failed",
                "error": error,
            }
        finally:
            if owns_turn:
                turn_steer.end_turn(session_key, turn_id)

        if isinstance(result, dict):
            report = dict(result.get("turn_report") or {})
            steer_summary = turn_steer.turn_summary(session_key, turn_id)
            if report or int(steer_summary.get("received") or 0):
                report["steer"] = steer_summary
                result["turn_report"] = report

        completed = bool(result.get("completed"))
        context_overflow = bool(result.get("context_overflow"))
        status = _turn_status(result, completed=completed, context_overflow=context_overflow)
        error = str(result.get("error") or "")
        final_response = _final_response_for_status(status, result.get("final_response", ""), error)
        outbound_message = _outbound_message_for_turn(
            final_response,
            agent if "agent" in locals() else None,
            include_artifacts=status == "completed" and completed,
        )
        was_compressed = bool(ctx.was_compressed) if ctx is not None else False
        should_review_memory = (
            bool(result.get("should_review_memory", ctx.should_review_memory))
            if ctx is not None and status == "completed"
            else False
        )

        persistence_summary: dict[str, Any] = {"partial": False}
        if status == "completed" and completed and not context_overflow:
            if was_compressed:
                stored_session_id = await self.session_store.create_compressed_session(
                    session_key, source, result["messages"]
                )
                if not stored_session_id:
                    stored_session_id = current_id
            else:
                await self.session_store.save_transcript(
                    current_id, result["messages"], previous_count
                )
                stored_session_id = current_id
        elif status == "stopped" and ctx is not None:
            partial = build_stopped_turn_transcript(
                list(result.get("messages") or ctx.messages),
                current_turn_user_idx=getattr(ctx, "current_turn_user_idx", None),
                user_text=user_input.text,
                stop_text=final_response,
            )
            await self.session_store.save_transcript(
                current_id, history + partial.messages, previous_count
            )
            stored_session_id = current_id
            persistence_summary = partial.summary
        else:
            minimal_messages = _minimal_turn_messages(user_input.text, final_response)
            await self.session_store.save_transcript(
                current_id, history + minimal_messages, previous_count
            )
            stored_session_id = current_id
        result["persistence"] = persistence_summary
        if isinstance(result.get("turn_report"), dict) and result["turn_report"]:
            result["turn_report"]["persistence"] = dict(persistence_summary)
        await emit_event(
            recorder,
            "turn_end",
            "会话已保存",
            session_key=session_key,
            status=status,
            completed=completed,
            was_compressed=was_compressed,
            context_overflow=context_overflow,
            partial=bool(persistence_summary.get("partial")),
            messages_saved=int(persistence_summary.get("messages_saved") or 0),
        )

        turn_report = dict(result.get("turn_report") or {})
        if turn_report:
            self.record_turn_report(session_key, source, turn_report)
            await self._record_turn_report(stored_session_id, session_key, source, turn_report)
        await self._record_tool_runs(
            stored_session_id,
            session_key,
            recorder.events,
            turn_id=str(turn_report.get("turn_id") or ""),
        )
        if status == "completed" and self.memory_review_service is not None:
            self.memory_review_service.submit(
                session_key=session_key,
                user_id=str(getattr(source, "user_id", "") or ""),
                messages=result.get("messages", []),
                turn_id=turn_id,
            )

        return ConversationTurnResult(
            final_response=final_response,
            messages=result.get("messages", []),
            completed=completed,
            context_overflow=context_overflow,
            was_compressed=was_compressed,
            should_review_memory=should_review_memory,
            raw=result,
            status=status,
            error=error,
            events=list(recorder.events),
            turn_report=turn_report,
            outbound_message=outbound_message,
        )

    def add_steer(self, session_key: str, source, text: str) -> SteerSignal:
        return self.steer_manager.add(session_key, source, text)

    def steer_snapshot(self, session_key: str | None = None) -> dict[str, Any]:
        return self.steer_manager.snapshot(session_key)

    def record_turn_report(self, session_key: str, source, report: dict[str, Any]) -> None:
        if not report:
            return
        self.turn_reports.append({
            "session_key": session_key,
            "source": _source_snapshot(source),
            "created_at": datetime.now(UTC).isoformat(),
            "status": str(report.get("status") or ""),
            "report": dict(report),
        })

    def recent_turn_reports(self, limit: int = 10) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        return list(self.turn_reports)[-limit:]

    def turn_report_summary(self) -> dict[str, Any]:
        if not self.turn_reports:
            return _empty_turn_report_summary()
        last = self.turn_reports[-1]
        report = last.get("report") or {}
        llm = report.get("llm") or {}
        tools = report.get("tools") or {}
        tool_truth = report.get("tool_truth") or {}
        assistant_claim = tool_truth.get("assistant_claim") or {}
        retries = report.get("retries") or []
        return {
            "stored": len(self.turn_reports),
            "last_status": str(report.get("status") or last.get("status") or ""),
            "last_error": str(report.get("error") or ""),
            "last_duration": float(report.get("duration") or 0.0),
            "last_llm_calls": int(llm.get("calls") or 0),
            "last_tool_calls": int(tools.get("total") or 0),
            "last_input_tokens": int(llm.get("input_tokens") or 0),
            "last_output_tokens": int(llm.get("output_tokens") or 0),
            "last_cache_hit_tokens": int(llm.get("cache_hit_tokens") or 0),
            "last_cache_miss_tokens": int(llm.get("cache_miss_tokens") or 0),
            "last_cache_write_tokens": int(llm.get("cache_write_tokens") or 0),
            "last_cache_read_tokens": int(llm.get("cache_read_tokens") or 0),
            "last_cache_hit_rate": float(llm.get("cache_hit_rate") or 0.0),
            "last_cache_diagnostics": dict(llm.get("cache_diagnostics") or {}),
            "last_retries": len(retries) if isinstance(retries, list) else 0,
            "last_tool_truth_warnings": list(tool_truth.get("warnings") or []),
            "last_claimed_but_no_tool_call": bool(
                assistant_claim.get("claimed_but_no_tool_call", False)
            ),
        }

    def recent_tool_truth(self, limit: int = 10) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        items: list[dict[str, Any]] = []
        for envelope in list(self.turn_reports)[-limit:]:
            report = envelope.get("report") or {}
            tool_truth = report.get("tool_truth") or {}
            assistant_claim = tool_truth.get("assistant_claim") or {}
            items.append({
                "created_at": str(envelope.get("created_at") or ""),
                "session_key": str(envelope.get("session_key") or ""),
                "source": dict(envelope.get("source") or {}),
                "status": str(report.get("status") or envelope.get("status") or ""),
                "user_message_summary": str(report.get("user_message_summary") or ""),
                "final_response_summary": str(report.get("final_response_summary") or ""),
                "calls_total": int(tool_truth.get("calls_total") or 0),
                "results_total": int(tool_truth.get("results_total") or 0),
                "llm_tool_call_count": int(tool_truth.get("llm_tool_call_count") or 0),
                "tool_names": list(tool_truth.get("tool_names") or []),
                "status_counts": _status_counts_snapshot(tool_truth.get("status_counts") or {}),
                "warnings": list(tool_truth.get("warnings") or []),
                "claimed_tool_use": bool(assistant_claim.get("claimed_tool_use", False)),
                "claimed_but_no_tool_call": bool(
                    assistant_claim.get("claimed_but_no_tool_call", False)
                ),
            })
        return items

    def tool_truth_summary(self, limit: int = 10) -> dict[str, Any]:
        recent = self.recent_tool_truth(limit)
        if not recent:
            return _empty_tool_truth_summary()

        tool_counts: dict[str, int] = {}
        status_counts = _status_counts_snapshot({})
        warning_counts: dict[str, int] = {}
        turns_with_tools = 0
        claim_mismatches = 0

        for item in recent:
            calls_total = int(item.get("calls_total") or 0)
            if calls_total > 0:
                turns_with_tools += 1
            if item.get("claimed_but_no_tool_call"):
                claim_mismatches += 1
            for name in item.get("tool_names") or []:
                name = str(name or "")
                if not name:
                    continue
                tool_counts[name] = tool_counts.get(name, 0) + 1
            for status, count in (item.get("status_counts") or {}).items():
                status_counts[status] = status_counts.get(status, 0) + int(count or 0)
            for warning in item.get("warnings") or []:
                warning = str(warning or "")
                if not warning:
                    continue
                warning_counts[warning] = warning_counts.get(warning, 0) + 1

        last = recent[-1]
        return {
            "stored": len(self.turn_reports),
            "inspected": len(recent),
            "turns_with_tools": turns_with_tools,
            "turns_without_tools": len(recent) - turns_with_tools,
            "claim_mismatches": claim_mismatches,
            "tool_counts": dict(sorted(tool_counts.items())),
            "status_counts": dict(sorted(status_counts.items())),
            "denied_tool_calls": int(status_counts.get("denied", 0)),
            "failed_tool_calls": int(status_counts.get("error", 0)),
            "warning_counts": dict(sorted(warning_counts.items())),
            "last_warning": str((last.get("warnings") or [""])[-1] or ""),
            "last_claimed_but_no_tool_call": bool(last.get("claimed_but_no_tool_call", False)),
        }

    async def recent_tool_runs(
        self,
        *,
        limit: int = 20,
        session_key: str | None = None,
        turn_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.session_store.recent_tool_runs(
            limit=limit,
            session_key=session_key,
            turn_id=turn_id,
        )

    async def get_tool_run(self, run_id: int) -> dict[str, Any] | None:
        return await self.session_store.get_tool_run(run_id)

    async def tool_run_summary(self, *, limit: int = 50) -> dict[str, Any]:
        return await self.session_store.tool_run_summary(limit=limit)

    async def recent_persisted_turn_reports(
        self,
        *,
        limit: int = 20,
        session_key: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.session_store.recent_turn_reports(
            limit=limit,
            session_key=session_key,
            status=status,
        )

    async def get_persisted_turn_report(self, report_id: int) -> dict[str, Any] | None:
        return await self.session_store.get_turn_report(report_id)

    async def tool_runs_for_turn_report(
        self,
        report_id: int,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        report = await self.get_persisted_turn_report(report_id)
        if not report:
            return []
        return await self.recent_tool_runs(
            limit=limit,
            session_key=str(report.get("session_key") or ""),
            turn_id=str(report.get("turn_id") or ""),
        )

    async def persisted_turn_report_summary(self) -> dict[str, Any]:
        try:
            summary = await self.session_store.turn_report_summary()
        except Exception as exc:
            import logging

            logging.getLogger(__name__).exception("Failed to summarize persisted turn reports")
            summary = _empty_persisted_turn_report_summary()
            summary["last_error"] = f"{type(exc).__name__}: {exc}"
            return summary
        self._apply_persisted_turn_summary(summary)
        return summary

    def tool_run_memory_summary(self, limit: int = 50) -> dict[str, Any]:
        recent = list(self._recent_tool_runs)[-limit:] if limit > 0 else []
        return _tool_run_summary(recent)

    def turn_report_persistence_summary(self) -> dict[str, Any]:
        if not self._last_persisted_turn_report:
            summary = _empty_persisted_turn_report_summary()
            summary["stored"] = self._persisted_turn_report_count
            summary["last_error"] = self._last_persisted_turn_report_error
            return summary
        summary = dict(self._last_persisted_turn_report)
        summary["stored"] = max(
            int(summary.get("stored") or 0),
            self._persisted_turn_report_count,
        )
        if self._last_persisted_turn_report_error:
            summary["last_error"] = self._last_persisted_turn_report_error
        return summary

    def _apply_persisted_turn_summary(self, summary: dict[str, Any]) -> None:
        self._last_persisted_turn_report = dict(summary)
        self._persisted_turn_report_count = int(summary.get("stored") or 0)
        self._last_persisted_turn_report_error = ""

    async def get_or_create_agent(self, session_key: str, *, capability_view=None):
        if capability_view is not None:
            from luna_agent.plugins.runtime import CapabilityKind

            capability_view = capability_view.project({
                CapabilityKind.TOOL,
                CapabilityKind.SKILL,
                CapabilityKind.WORKFLOW,
            })
        agent = self.agent_cache.get(session_key)
        if agent is not None:
            from luna_agent.tools.registry import tool_registry

            if capability_view is not None:
                from luna_agent.agent.agent import _build_system_prompt, _refresh_tools

                agent._capability_view = capability_view
                previous = agent._capability_fingerprint
                _refresh_tools(agent)
                if previous != agent._capability_fingerprint:
                    _build_system_prompt(agent, agent._system_prompt_template)
                self.agent_cache.move_to_end(session_key)
                self._apply_security_context(agent, session_key)
                agent._artifact_store = self.artifact_store
                return agent

            if agent._tools_generation == tool_registry.generation:
                self.agent_cache.move_to_end(session_key)
                self._apply_security_context(agent, session_key)
                agent._artifact_store = self.artifact_store
                return agent
            del self.agent_cache[session_key]

        from luna_agent.agent.factory import create_agent_runtime

        runtime_kwargs = {
            "memory_manager": self.memory_manager,
            "plugin_manager": self.plugin_manager,
            "system_prompt_template": self.system_prompt_template,
            "session_key": session_key,
        }
        if capability_view is not None and _accepts_parameter(
            create_agent_runtime,
            "capability_view",
        ):
            runtime_kwargs["capability_view"] = capability_view
        runtime = await create_agent_runtime(self.settings, **runtime_kwargs)
        agent = runtime.agent
        agent._artifact_store = self.artifact_store
        self._apply_security_context(agent, session_key)
        if self.agent_cache_max is not None:
            while len(self.agent_cache) >= self.agent_cache_max:
                self.agent_cache.popitem(last=False)
        self.agent_cache[session_key] = agent
        return agent

    def security_context(self, session_key: str):
        return self.security_states.context(session_key)

    def set_security_mode(self, session_key: str, mode: object):
        return self.security_states.set_mode(session_key, mode)

    def capture_turn_policy(self, session_key: str):
        from luna_agent.conversation.policy import TurnPolicySnapshot

        state = self.security_states.get(session_key)
        return TurnPolicySnapshot.capture(
            session_key,
            revision=state.revision,
            security=self.security_context(session_key),
        )

    def _apply_security_context(self, agent, session_key: str) -> None:
        context = self.security_context(session_key)
        agent._security_context = context
        agent._security_grant_ttl_seconds = self.security_states.grant_ttl_seconds

    async def _record_tool_runs(
        self,
        session_id: str,
        session_key: str,
        events: list[ConversationEvent],
        *,
        turn_id: str = "",
    ) -> None:
        runs = _tool_runs_from_events(
            session_id,
            session_key,
            events,
            turn_id=turn_id,
        )
        if not runs:
            return
        try:
            await self.session_store.save_tool_runs(runs)
        except Exception:
            import logging

            logging.getLogger(__name__).exception("Failed to persist tool runs")
            return
        self._recent_tool_runs.extend(runs)

    async def _record_turn_report(
        self,
        session_id: str,
        session_key: str,
        source,
        report: dict[str, Any],
    ) -> None:
        envelope = _turn_report_envelope(session_id, session_key, source, report)
        try:
            report_id = await self.session_store.save_turn_report(envelope)
        except Exception:
            import logging

            logging.getLogger(__name__).exception("Failed to persist turn report")
            self._last_persisted_turn_report_error = "persist_failed"
            return
        self._persisted_turn_report_count += 1
        summary = _persisted_turn_report_summary_from_envelope(envelope, report_id)
        summary["stored"] = self._persisted_turn_report_count
        self._apply_persisted_turn_summary(summary)

    async def load_history(self, session_key: str, source) -> list[dict]:
        session = await self.session_store.get_or_create(session_key, source)
        current_id = self.resolve_session_id(session.session_id)
        return await self.session_store.load_history(current_id)

    async def ensure_session(self, session_key: str, source) -> None:
        await self.session_store.get_or_create(session_key, source)

    async def reset_session(self, session_key: str, source) -> str:
        new_id = await self.session_store.reset_session(session_key, source)
        self._pending_session_start_sources[session_key] = "clear"
        self.clear_agent(session_key)
        self.security_states.clear(session_key)
        return new_id

    async def _conversation_hook_contexts(
        self,
        *,
        session_key: str,
        session_id: str,
        turn_id: str,
        source,
        user_input: ConversationInput,
        previous_session_id: str | None,
    ) -> list[str]:
        if self.hook_manager is None:
            return []
        from luna_agent.hooks import HookEnvelope, HookEvent, HookScope, HookSourceContext

        common = {
            "scope": HookScope.TURN,
            "session_key": session_key,
            "turn_id": turn_id,
            "cwd": str(Path.cwd()),
            "source": HookSourceContext(
                platform=str(getattr(source, "platform", "") or ""),
                user_id=str(getattr(source, "user_id", "") or ""),
                chat_id=str(getattr(source, "chat_id", "") or ""),
            ),
        }
        contexts: list[str] = []
        if session_id not in self._hook_started_session_ids:
            start_source = self._pending_session_start_sources.pop(session_key, "")
            if not start_source:
                start_source = "new" if previous_session_id is None else "resume"
            outcome = await self.hook_manager.dispatch(HookEnvelope(
                event_name=HookEvent.SESSION_START,
                payload={"source": start_source, "session_id": session_id},
                **common,
            ))
            if outcome.additional_context.strip():
                contexts.append(f"[SessionStart hook context]\n{outcome.additional_context.strip()}")
            if outcome.stop:
                raise _HookTurnStopped(outcome.reason or "session start blocked by hook")
            self._hook_started_session_ids.add(session_id)

        prompt_outcome = await self.hook_manager.dispatch(HookEnvelope(
            event_name=HookEvent.USER_PROMPT_SUBMIT,
            payload={
                "text": user_input.text,
                "attachment_count": len(user_input.attachments),
            },
            **common,
        ))
        if prompt_outcome.additional_context.strip():
            contexts.append(
                f"[UserPromptSubmit hook context]\n{prompt_outcome.additional_context.strip()}"
            )
        if prompt_outcome.stop:
            raise _HookTurnStopped(prompt_outcome.reason or "user prompt blocked by hook")
        return contexts

    async def rename_session(self, old_key: str, new_key: str) -> bool:
        ok = await self.session_store.rename_session(old_key, new_key)
        if ok:
            self.move_agent(old_key, new_key)
            self.security_states.move(old_key, new_key)
        return ok

    async def delete_session(self, session_key: str) -> bool:
        if self.session_store.get(session_key) is None:
            return False
        await self.session_store.delete_session(session_key)
        self.delete_agent(session_key)
        self.security_states.clear(session_key)
        return True

    async def session_list_summary(
        self,
        *,
        platform: str,
        user_id: str,
        current_key: str,
        limit: int = 10,
    ) -> str:
        sessions = await self.session_store.list_user_sessions(platform, user_id)
        lines = [f"当前会话: {current_key}", "你的会话列表:"]
        for item in sessions[:limit]:
            marker = " <-" if item["session_key"] == current_key else ""
            lines.append(f"  {item['session_key']}{marker} ({item.get('message_count', 0)} 条消息)")
        if len(lines) == 2:
            lines.append("  无")
        return "\n".join(lines)

    async def current_session_summary(self, session_key: str, source) -> str:
        session = await self.session_store.get_or_create(session_key, source)
        current_id = self.resolve_session_id(session.session_id)
        count = len(await self.session_store.load_history(current_id))
        return (
            f"当前会话: {session_key}\n"
            f"session id: {current_id[:8]}\n"
            f"消息数: {count}"
        )

    async def export_session(self, session_key: str, source, export_path: Path) -> int:
        session = await self.session_store.get_or_create(session_key, source)
        current_id = self.resolve_session_id(session.session_id)
        return await self.session_store.export(current_id, str(export_path))

    def default_export_path(self, session_key: str) -> Path:
        return self.settings.agent_data_dir / "exports" / f"{session_key.replace(':', '_')}.jsonl"

    async def usage_summary(
        self,
        session_key: str,
        source,
        *,
        current_user_message: str = "",
        create_agent: bool = True,
        empty_message: str | None = None,
    ) -> str:
        agent = self.agent_cache.get(session_key)
        if agent is None:
            if not create_agent:
                return empty_message or "暂无会话数据。"
            agent = await self.get_or_create_agent(session_key)

        history = await self.load_history(session_key, source)

        from luna_agent.context_budget import build_context_budget
        from luna_agent.context_budget import compose_context_text

        budget = await build_context_budget(
            messages=history,
            agent=agent,
            settings=self.settings,
            skills_summary=compose_context_text(
                getattr(agent, "_last_skill_summaries", ""),
                getattr(agent, "_last_skill_injection", ""),
            ),
            memory_injections=getattr(agent, "_last_memory_injections", ""),
            current_user_message=current_user_message,
        )
        threshold_line = ""
        if budget.compression_threshold:
            marker = "，已达到" if budget.over_compression_threshold else ""
            threshold_line = f"压缩阈值: {budget.compression_threshold:,} tokens{marker}\n"
        recent_tool_calls = len(getattr(agent, "_last_tool_results", []) or [])
        max_tool_calls = int(getattr(agent, "_max_tool_calls_per_turn", 0) or 0)
        return (
            f"会话用量\n"
            f"API 调用: {agent.session_api_calls} 次\n"
            f"输入 tokens: {agent.session_prompt_tokens:,} (API 报告)\n"
            f"输出 tokens: {agent.session_completion_tokens:,} (API 报告)\n"
            f"\n上下文窗口 (估算)\n"
            f"已用: {budget.used:,} / {budget.context_limit:,} tokens ({budget.percent}%)\n"
            f"  system prompt: {budget.system_prompt:,}\n"
            f"  history messages: {budget.history_messages:,}\n"
            f"  tools schema: {budget.tools_schema:,}\n"
            f"  skills: {budget.skills:,}\n"
            f"  memory injections: {budget.memory_injections:,}\n"
            f"  MCP tools: {budget.mcp_tools:,}\n"
            f"剩余: {budget.remaining_context:,} tokens\n"
            f"{threshold_line}"
            f"\n最近一轮工具执行: {recent_tool_calls} 次\n"
            f"单轮工具上限: {max_tool_calls} 次"
        )

    def get_cached_agent(self, session_key: str):
        return self.agent_cache.get(session_key)

    def has_cached_agent(self, session_key: str) -> bool:
        return session_key in self.agent_cache

    def invalidate_agent(self, session_key: str) -> None:
        self.agent_cache.pop(session_key, None)

    def rename_cached_agent(self, old_key: str, new_key: str) -> None:
        if old_key == new_key:
            return
        agent = self.agent_cache.pop(old_key, None)
        if agent is not None:
            self.agent_cache[new_key] = agent

    def iter_cached_agents(self):
        return iter(self.agent_cache.values())

    def request_stop(self, session_key: str | None = None) -> int:
        agents = (
            [self.get_cached_agent(session_key)]
            if session_key is not None
            else list(self.iter_cached_agents())
        )
        for agent in agents:
            if agent is not None and hasattr(agent, "_interrupt_requested"):
                agent._interrupt_requested = True

        from luna_agent.tools.executor import interrupt_active_tool_executions
        from luna_agent.plugins.builtin.tools.builtin.delegate import stop_delegate_agents

        interrupt_active_tool_executions()
        return int(stop_delegate_agents() or 0)

    def resolve_session_id(self, session_id: str) -> str:
        if self.compression_chain is None:
            return session_id
        return self.compression_chain.resolve(session_id)

    def clear_agent(self, session_key: str) -> None:
        self.invalidate_agent(session_key)

    def delete_agent(self, session_key: str) -> None:
        self.invalidate_agent(session_key)

    def move_agent(self, old_key: str, new_key: str) -> None:
        self.rename_cached_agent(old_key, new_key)

    def stop_all_agents(self) -> int:
        return self.request_stop(None)

def _minimal_turn_messages(user_text: str, assistant_text: str) -> list[dict]:
    return [
        {"role": "user", "content": [{"type": "text", "text": user_text}]},
        {"role": "assistant", "content": [{"type": "text", "text": assistant_text}]},
    ]


def _source_snapshot(source) -> dict[str, str]:
    return {
        "platform": str(getattr(source, "platform", "") or ""),
        "user_id": str(getattr(source, "user_id", "") or ""),
        "chat_id": str(getattr(source, "chat_id", "") or ""),
        "chat_type": str(getattr(source, "chat_type", "") or ""),
    }


def _turn_report_envelope(
    session_id: str,
    session_key: str,
    source,
    report: dict[str, Any],
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "session_key": session_key,
        "source": _source_snapshot(source),
        "created_at": time.time(),
        "status": str(report.get("status") or ""),
        "turn_id": str(report.get("turn_id") or ""),
        "report": dict(report),
    }


def _empty_turn_report_summary() -> dict[str, Any]:
    return {
        "stored": 0,
        "last_status": "",
        "last_error": "",
        "last_duration": 0.0,
        "last_llm_calls": 0,
        "last_tool_calls": 0,
        "last_input_tokens": 0,
        "last_output_tokens": 0,
        "last_cache_hit_tokens": 0,
        "last_cache_miss_tokens": 0,
        "last_cache_write_tokens": 0,
        "last_cache_read_tokens": 0,
        "last_cache_hit_rate": 0.0,
        "last_cache_diagnostics": {},
        "last_retries": 0,
        "last_tool_truth_warnings": [],
        "last_claimed_but_no_tool_call": False,
    }


def _empty_persisted_turn_report_summary() -> dict[str, Any]:
    return {
        "stored": 0,
        "last_id": 0,
        "last_turn_id": "",
        "last_session_key": "",
        "last_status": "",
        "last_error": "",
        "last_duration": 0.0,
        "last_llm_calls": 0,
        "last_tool_calls": 0,
        "last_cache_hit_tokens": 0,
        "last_cache_miss_tokens": 0,
        "last_cache_write_tokens": 0,
        "last_cache_read_tokens": 0,
    }


def _persisted_turn_report_summary_from_envelope(
    envelope: dict[str, Any],
    report_id: int,
) -> dict[str, Any]:
    report = envelope.get("report") if isinstance(envelope.get("report"), dict) else {}
    llm = report.get("llm") if isinstance(report.get("llm"), dict) else {}
    tools = report.get("tools") if isinstance(report.get("tools"), dict) else {}
    return {
        "stored": 1,
        "last_id": int(report_id or 0),
        "last_turn_id": str(report.get("turn_id") or envelope.get("turn_id") or ""),
        "last_session_key": str(envelope.get("session_key") or ""),
        "last_status": str(report.get("status") or envelope.get("status") or ""),
        "last_error": str(report.get("error") or ""),
        "last_duration": _as_float(report.get("duration")),
        "last_llm_calls": _as_int(llm.get("calls")),
        "last_tool_calls": _as_int(tools.get("total")),
        "last_cache_hit_tokens": _as_int(llm.get("cache_hit_tokens")),
        "last_cache_miss_tokens": _as_int(llm.get("cache_miss_tokens")),
        "last_cache_write_tokens": _as_int(llm.get("cache_write_tokens")),
        "last_cache_read_tokens": _as_int(llm.get("cache_read_tokens")),
    }


def _empty_tool_truth_summary() -> dict[str, Any]:
    return {
        "stored": 0,
        "inspected": 0,
        "turns_with_tools": 0,
        "turns_without_tools": 0,
        "claim_mismatches": 0,
        "tool_counts": {},
        "status_counts": _status_counts_snapshot({}),
        "denied_tool_calls": 0,
        "failed_tool_calls": 0,
        "warning_counts": {},
        "last_warning": "",
        "last_claimed_but_no_tool_call": False,
    }


def _tool_runs_from_events(
    session_id: str,
    session_key: str,
    events: list[ConversationEvent],
    *,
    turn_id: str = "",
) -> list[dict[str, Any]]:
    resolved_turn_id = turn_id
    for event in events:
        if event.type == "turn_start":
            resolved_turn_id = str(event.data.get("turn_id") or resolved_turn_id)
            break
    created_at = time.time()
    runs: list[dict[str, Any]] = []
    for event in events:
        if event.type != "tool_end":
            continue
        data = event.data
        if data.get("count_as_tool") is False:
            continue
        runs.append({
            "session_id": session_id,
            "session_key": session_key,
            "turn_id": resolved_turn_id,
            "tool_use_id": str(data.get("tool_use_id") or data.get("tool_name") or "tool"),
            "tool_name": str(data.get("tool_name") or ""),
            "status": str(data.get("status") or ""),
            "category": str(data.get("category") or ""),
            "duration": _as_float(data.get("duration")),
            "input_summary": str(data.get("input_summary") or ""),
            "output_summary": str(data.get("output_summary") or ""),
            "full_output": str(data.get("full_output") or ""),
            "output_truncated": bool(data.get("output_truncated", False)),
            "artifacts": list(data.get("artifacts") or []),
            "result_metadata": dict(data.get("result_metadata") or {}),
            "error": str(data.get("error") or ""),
            "guard_stage": str(data.get("guard_stage") or ""),
            "reason_code": str(data.get("guard_reason_code") or data.get("reason_code") or ""),
            "permission_category": str(data.get("permission_category") or ""),
            "permission_decision": str(data.get("permission_decision") or ""),
            "required_allow": str(data.get("required_allow") or ""),
            "execution_mode": str(data.get("execution_mode") or ""),
            "grant_matched": str(data.get("grant_matched") or ""),
            "grant_scope": str(data.get("grant_scope") or ""),
            "grant_expires_at": _as_float(data.get("grant_expires_at")),
            "temporary_grant_ttl_seconds": int(_as_float(data.get("temporary_grant_ttl_seconds"))),
            "created_at": created_at,
        })
    return runs


def _tool_run_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return _empty_tool_run_summary()
    tool_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    truncated = 0
    for item in items:
        tool_name = str(item.get("tool_name") or "")
        status = str(item.get("status") or "")
        category = str(item.get("category") or "")
        if tool_name:
            tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
        if status:
            status_counts[status] = status_counts.get(status, 0) + 1
        if category:
            category_counts[category] = category_counts.get(category, 0) + 1
        if item.get("output_truncated"):
            truncated += 1
    return {
        "inspected": len(items),
        "tool_counts": dict(sorted(tool_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "category_counts": dict(sorted(category_counts.items())),
        "denied": int(status_counts.get("denied", 0)),
        "failed": int(status_counts.get("error", 0)),
        "timeouts": int(status_counts.get("timeout", 0)),
        "truncated": truncated,
    }


def _empty_tool_run_summary() -> dict[str, Any]:
    return {
        "inspected": 0,
        "tool_counts": {},
        "status_counts": {},
        "category_counts": {},
        "denied": 0,
        "failed": 0,
        "timeouts": 0,
        "truncated": 0,
    }


def _status_counts_snapshot(value: Any) -> dict[str, int]:
    counts = {
        "success": 0,
        "error": 0,
        "denied": 0,
        "timeout": 0,
        "interrupted": 0,
        "skipped": 0,
    }
    if not isinstance(value, dict):
        return counts
    for key, raw in value.items():
        try:
            counts[str(key)] = int(raw or 0)
        except (TypeError, ValueError):
            counts[str(key)] = 0
    return counts


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _turn_status(result: dict[str, Any], *, completed: bool, context_overflow: bool) -> str:
    raw = str(result.get("status") or "").strip()
    if raw in {"completed", "stopped", "failed", "context_overflow"}:
        return raw
    if context_overflow:
        return "context_overflow"
    if not completed:
        if str(result.get("final_response") or "").strip() == "已停止。":
            return "stopped"
        return "failed"
    return "completed"


def _final_response_for_status(status: str, final_response: Any, error: str = "") -> str:
    text = str(final_response or "").strip()
    if text:
        return text
    if status == "stopped":
        return "已停止。"
    if status == "context_overflow":
        return "抱歉，本轮上下文超出限制，未能完成处理。"
    if status == "failed":
        return f"抱歉，本轮处理出错了：{error}" if error else "抱歉，本轮处理出错了。"
    return EMPTY_FINAL_RESPONSE_MESSAGE


def _outbound_message_for_turn(final_response: str, agent, *, include_artifacts: bool) -> OutboundMessage:
    parts = [MessagePart(type="text", text=str(final_response or ""))]
    draft = getattr(agent, "_response_draft", None) if agent is not None else None
    selected = list(getattr(draft, "selected", []) or []) if include_artifacts else []
    for ref in selected:
        parts.append(MessagePart(
            type=str(getattr(ref, "kind", "") or "file"),
            artifact_id=str(getattr(ref, "artifact_id", "") or ""),
            name=str(getattr(ref, "filename", "") or ""),
            mime_type=str(getattr(ref, "mime_type", "") or ""),
            metadata={"size_bytes": int(getattr(ref, "size_bytes", 0) or 0)},
        ))
    return OutboundMessage(parts=parts)


def _accepts_event_sink(func: Any) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return True
    return "event_sink" in params or any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in params.values()
    )


def _accepts_confirm(func: Any) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return True
    return "confirm" in params or any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in params.values()
    )


def _accepts_steer(func: Any) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return True
    return "steer" in params or any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in params.values()
    )


def _accepts_turn_id(func: Any) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return True
    return "turn_id" in params or any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in params.values()
    )


def _accepts_parameter(func: Any, name: str) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return True
    return name in params or any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in params.values()
    )


def _event_detail_id(category: str, detail: str) -> str:
    digest = hashlib.sha1(f"{category}:{detail}".encode("utf-8", errors="replace")).hexdigest()
    return f"err_{digest[:12]}"
