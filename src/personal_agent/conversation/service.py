"""Shared conversation runtime for CLI and Gateway entrypoints."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ConversationTurnResult:
    final_response: str
    messages: list[dict]
    completed: bool
    context_overflow: bool
    was_compressed: bool
    should_review_memory: bool
    raw: dict[str, Any]


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

    async def run_turn(self, session_key: str, source, text: str) -> ConversationTurnResult:
        if self.plugin_manager is not None:
            await self.plugin_manager.invoke_hook("on_session_selected", session_key=session_key)

        session = await self.session_store.get_or_create(session_key, source)
        current_id = self.resolve_session_id(session.session_id)
        history = await self.session_store.load_history(current_id)
        previous_count = len(history)
        agent = await self.get_or_create_agent(session_key)

        from personal_agent.agent.context import build_turn_context
        from personal_agent.agent.loop import run_conversation

        ctx = await build_turn_context(agent, text, history)
        result = await run_conversation(agent, ctx)

        completed = bool(result.get("completed"))
        context_overflow = bool(result.get("context_overflow"))
        if completed and not context_overflow:
            if ctx.was_compressed:
                await self.session_store.create_compressed_session(
                    session_key, source, result["messages"]
                )
            else:
                await self.session_store.save_transcript(
                    current_id, result["messages"], previous_count
                )

        return ConversationTurnResult(
            final_response=result.get("final_response", "") or "",
            messages=result.get("messages", []),
            completed=completed,
            context_overflow=context_overflow,
            was_compressed=ctx.was_compressed,
            should_review_memory=bool(result.get("should_review_memory", ctx.should_review_memory)),
            raw=result,
        )

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

        budget = await build_context_budget(
            messages=history,
            agent=agent,
            settings=self.settings,
            skills_summary="\n".join(
                part for part in (
                    getattr(agent, "_last_skill_summaries", ""),
                    getattr(agent, "_last_skill_injection", ""),
                )
                if part
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

    def resolve_session_id(self, session_id: str) -> str:
        if self.compression_chain is None:
            return session_id
        return self.compression_chain.resolve(session_id)

    def clear_agent(self, session_key: str) -> None:
        self.agent_cache.pop(session_key, None)

    def delete_agent(self, session_key: str) -> None:
        self.clear_agent(session_key)

    def move_agent(self, old_key: str, new_key: str) -> None:
        if old_key == new_key:
            return
        agent = self.agent_cache.pop(old_key, None)
        if agent is not None:
            self.agent_cache[new_key] = agent

    def stop_all_agents(self) -> int:
        for agent in self.agent_cache.values():
            if hasattr(agent, "_interrupt_requested"):
                agent._interrupt_requested = True
        from personal_agent.tools.executor import set_interrupted
        from personal_agent.plugins.builtin.tools.builtin.delegate import stop_delegate_agents

        set_interrupted()
        return int(stop_delegate_agents() or 0)
