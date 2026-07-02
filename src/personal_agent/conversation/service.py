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

    async def export_session(self, session_key: str, source, export_path: Path) -> int:
        session = await self.session_store.get_or_create(session_key, source)
        current_id = self.resolve_session_id(session.session_id)
        return await self.session_store.export(current_id, str(export_path))

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
