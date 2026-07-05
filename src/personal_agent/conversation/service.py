"""Shared conversation runtime for CLI and Gateway entrypoints."""

from __future__ import annotations

import inspect
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from personal_agent.conversation.events import ConversationEvent, EventRecorder, emit_event

TURN_REPORT_HISTORY_LIMIT = 50


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


class ConversationService:
    def __init__(
        self,
        *,
        settings,
        plugin_manager,
        session_store,
        compression_chain,
        memory_manager,
        system_prompt_template: str = "",
        agent_cache: dict[str, object] | OrderedDict[str, object] | None = None,
        agent_cache_max: int | None = None,
    ) -> None:
        self.settings = settings
        self.plugin_manager = plugin_manager
        self.session_store = session_store
        self.compression_chain = compression_chain
        self.memory_manager = memory_manager
        self.system_prompt_template = system_prompt_template
        self.agent_cache_max = agent_cache_max
        self.agent_cache: OrderedDict[str, object] = (
            agent_cache
            if isinstance(agent_cache, OrderedDict)
            else OrderedDict(agent_cache or {})
        )
        self.turn_reports: deque[dict[str, Any]] = deque(maxlen=TURN_REPORT_HISTORY_LIMIT)

    async def run_turn(self, session_key: str, source, text: str) -> ConversationTurnResult:
        return await self.run_turn_events(session_key, source, text)

    async def run_turn_events(
        self,
        session_key: str,
        source,
        text: str,
        *,
        event_sink=None,
    ) -> ConversationTurnResult:
        recorder = EventRecorder(event_sink)
        if self.plugin_manager is not None:
            await self.plugin_manager.invoke_hook("on_session_selected", session_key=session_key)

        session = await self.session_store.get_or_create(session_key, source)
        current_id = self.resolve_session_id(session.session_id)
        history = await self.session_store.load_history(current_id)
        previous_count = len(history)

        from personal_agent.agent.context import build_turn_context
        from personal_agent.agent.loop import run_conversation

        ctx = None
        try:
            agent = await self.get_or_create_agent(session_key)
            ctx = await build_turn_context(agent, text, history)
            if _accepts_event_sink(run_conversation):
                result = await run_conversation(agent, ctx, event_sink=recorder)
            else:
                result = await run_conversation(agent, ctx)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            final = f"抱歉，本轮处理出错了：{exc}"
            await emit_event(recorder, "error", "本轮处理失败", error=error)
            result = {
                "final_response": final,
                "messages": _minimal_turn_messages(text, final),
                "completed": False,
                "status": "failed",
                "error": error,
            }

        completed = bool(result.get("completed"))
        context_overflow = bool(result.get("context_overflow"))
        status = _turn_status(result, completed=completed, context_overflow=context_overflow)
        error = str(result.get("error") or "")
        final_response = _final_response_for_status(status, result.get("final_response", ""), error)
        was_compressed = bool(ctx.was_compressed) if ctx is not None else False
        should_review_memory = (
            bool(result.get("should_review_memory", ctx.should_review_memory))
            if ctx is not None and status == "completed"
            else False
        )

        if status == "completed" and completed and not context_overflow:
            if was_compressed:
                await self.session_store.create_compressed_session(
                    session_key, source, result["messages"]
                )
            else:
                await self.session_store.save_transcript(
                    current_id, result["messages"], previous_count
                )
        else:
            minimal_messages = _minimal_turn_messages(text, final_response)
            await self.session_store.save_transcript(
                current_id, history + minimal_messages, previous_count
            )
        await emit_event(
            recorder,
            "turn_end",
            "会话已保存",
            session_key=session_key,
            status=status,
            completed=completed,
            was_compressed=was_compressed,
            context_overflow=context_overflow,
        )

        turn_report = dict(result.get("turn_report") or {})
        if turn_report:
            self.record_turn_report(session_key, source, turn_report)

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
        )

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
            "last_retries": len(retries) if isinstance(retries, list) else 0,
            "last_tool_truth_warnings": list(tool_truth.get("warnings") or []),
            "last_claimed_but_no_tool_call": bool(
                assistant_claim.get("claimed_but_no_tool_call", False)
            ),
        }

    async def get_or_create_agent(self, session_key: str):
        agent = self.agent_cache.get(session_key)
        if agent is not None:
            from personal_agent.tools.registry import tool_registry

            if agent._tools_generation == tool_registry.generation:
                self.agent_cache.move_to_end(session_key)
                return agent
            del self.agent_cache[session_key]

        from personal_agent.agent.factory import create_agent_runtime

        runtime = await create_agent_runtime(
            self.settings,
            memory_manager=self.memory_manager,
            plugin_manager=self.plugin_manager,
            system_prompt_template=self.system_prompt_template,
        )
        agent = runtime.agent
        if self.agent_cache_max is not None:
            while len(self.agent_cache) >= self.agent_cache_max:
                self.agent_cache.popitem(last=False)
        self.agent_cache[session_key] = agent
        return agent

    async def load_history(self, session_key: str, source) -> list[dict]:
        session = await self.session_store.get_or_create(session_key, source)
        current_id = self.resolve_session_id(session.session_id)
        return await self.session_store.load_history(current_id)

    async def ensure_session(self, session_key: str, source) -> None:
        await self.session_store.get_or_create(session_key, source)

    async def reset_session(self, session_key: str, source) -> str:
        new_id = await self.session_store.reset_session(session_key, source)
        self.clear_agent(session_key)
        return new_id

    async def rename_session(self, old_key: str, new_key: str) -> bool:
        ok = await self.session_store.rename_session(old_key, new_key)
        if ok:
            self.move_agent(old_key, new_key)
        return ok

    async def delete_session(self, session_key: str) -> bool:
        if self.session_store.get(session_key) is None:
            return False
        await self.session_store.delete_session(session_key)
        self.delete_agent(session_key)
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

        from personal_agent.context_budget import build_context_budget
        from personal_agent.context_budget import compose_context_text

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
            f"\n本轮工具调用: {agent._tool_calls_this_turn} / {agent._max_tool_calls_per_turn}"
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

    def allow_agent_category(self, session_key: str, category: str) -> bool:
        agent = self.get_cached_agent(session_key)
        if agent is None:
            return False
        self._allow_agent_category(agent, category)
        return True

    def allow_all_cached_agents(self, category: str) -> int:
        count = 0
        for agent in self.iter_cached_agents():
            self._allow_agent_category(agent, category)
            count += 1
        return count

    def request_stop(self, session_key: str | None = None) -> int:
        agents = (
            [self.get_cached_agent(session_key)]
            if session_key is not None
            else list(self.iter_cached_agents())
        )
        for agent in agents:
            if agent is not None and hasattr(agent, "_interrupt_requested"):
                agent._interrupt_requested = True

        from personal_agent.tools.executor import interrupt_active_tool_executions
        from personal_agent.plugins.builtin.tools.builtin.delegate import stop_delegate_agents

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

    @staticmethod
    def _allow_agent_category(agent, category: str) -> None:
        if hasattr(agent, "_destructive_allowed"):
            agent._destructive_allowed.add(category)


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
        "last_retries": 0,
        "last_tool_truth_warnings": [],
        "last_claimed_but_no_tool_call": False,
    }


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
    return ""


def _accepts_event_sink(func: Any) -> bool:
    try:
        return "event_sink" in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return True
