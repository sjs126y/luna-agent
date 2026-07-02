"""Interactive persistent CLI chat runtime."""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import Callable

from personal_agent.config import Settings
from personal_agent.db.database import Database
from personal_agent.gateway.compression_chain import CompressionChain
from personal_agent.gateway.session_store import SessionStore
from personal_agent.memory.manager import MemoryManager
from personal_agent.models.messages import SessionSource
from personal_agent.runtime import AppRuntime, create_app_runtime

logger = logging.getLogger(__name__)

CLI_SYSTEM_PROMPT = (
    "你是一个智能助手。优先使用工具获取实时信息和执行操作，不要凭记忆编造。用中文回复。"
)


@dataclass
class CliChatRuntime:
    settings: Settings
    plugin_manager: object
    db: Database
    session_store: SessionStore
    compression_chain: CompressionChain
    memory_manager: MemoryManager
    mcp_manager: object | None = None
    app_runtime: AppRuntime | None = None
    system_prompt_template: str = CLI_SYSTEM_PROMPT
    session_name: str = "default"
    agent_cache: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.agent_cache is None:
            self.agent_cache = {}

    @property
    def source(self) -> SessionSource:
        return SessionSource(
            platform="cli",
            user_id="local",
            user_name="CLI",
            chat_id=self.session_name,
            chat_type="dm",
        )

    @property
    def session_key(self) -> str:
        return f"cli:{self.session_name}:local"

    @property
    def plugin_command_scopes(self) -> tuple[str, str]:
        return ("cli", "slash")

    async def close(self) -> None:
        if self.app_runtime is not None:
            await self.app_runtime.close()
            return
        if self.mcp_manager is not None:
            await self.mcp_manager.stop()
        await self.db.close()

    async def run_once(self, text: str) -> str:
        command_result = await self.handle_command(text)
        if command_result is not None:
            return command_result
        return await self.run_message(text)

    async def repl(
        self,
        *,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ) -> None:
        output_fn(f"Personal Agent CLI。当前会话: {self.session_key}。输入 exit/quit 或空行退出，/help 查看命令。")
        while True:
            try:
                text = input_fn(f"{self.session_key} >>> ")
            except (EOFError, KeyboardInterrupt):
                output_fn("")
                break
            text = text.strip()
            if not text or text.lower() in {"exit", "quit"}:
                break
            try:
                response = await self.run_once(text)
            except Exception as exc:
                logger.exception("CLI turn failed")
                output_fn(f"错误: 本轮对话失败: {exc}")
                continue
            if response:
                output_fn(response)

    async def run_message(self, text: str) -> str:
        await self.plugin_manager.invoke_hook("on_session_selected", session_key=self.session_key)
        session = await self.session_store.get_or_create(self.session_key, self.source)
        current_id = self.compression_chain.resolve(session.session_id)
        history = await self.session_store.load_history(current_id)
        previous_count = len(history)
        agent = await self.get_or_create_agent()

        from personal_agent.agent.context import build_turn_context
        from personal_agent.agent.loop import run_conversation

        ctx = await build_turn_context(agent, text, history)
        result = await run_conversation(agent, ctx)

        if result.get("completed") and not result.get("context_overflow"):
            if ctx.was_compressed:
                await self.session_store.create_compressed_session(
                    self.session_key, self.source, result["messages"]
                )
            else:
                await self.session_store.save_transcript(
                    current_id, result["messages"], previous_count
                )

        return result.get("final_response", "") or "..."

    async def get_or_create_agent(self):
        assert self.agent_cache is not None
        key = self.session_key
        if key in self.agent_cache:
            agent = self.agent_cache[key]
            from personal_agent.tools.registry import tool_registry

            if agent._tools_generation == tool_registry.generation:
                return agent
            del self.agent_cache[key]

        from personal_agent.agent.factory import create_agent_runtime

        runtime = await create_agent_runtime(
            self.settings,
            memory_manager=self.memory_manager,
            plugin_manager=self.plugin_manager,
            system_prompt_template=self.system_prompt_template,
        )
        self.agent_cache[key] = runtime.agent
        return runtime.agent

    async def reset_session(self) -> str:
        await self.session_store.reset_session(self.session_key, self.source)
        return "会话已重置。开始新的对话吧。"

    async def clear_agent(self) -> None:
        assert self.agent_cache is not None
        self.agent_cache.pop(self.session_key, None)

    async def switch_session(self, name: str) -> str:
        self.session_name = _clean_session_name(name)
        await self.session_store.get_or_create(self.session_key, self.source)
        return f"会话已切换: {self.session_key}"

    async def list_sessions(self) -> str:
        sessions = await self.session_store.list_user_sessions("cli", "local")
        lines = [f"当前会话: {self.session_key}", "你的会话列表:"]
        for item in sessions[:10]:
            marker = " <-" if item["session_key"] == self.session_key else ""
            lines.append(f"  {item['session_key']}{marker} ({item.get('message_count', 0)} 条消息)")
        if len(lines) == 2:
            lines.append("  无")
        return "\n".join(lines)

    async def current_session(self) -> str:
        session = await self.session_store.get_or_create(self.session_key, self.source)
        current_id = self.compression_chain.resolve(session.session_id)
        count = len(await self.session_store.load_history(current_id))
        return (
            f"当前会话: {self.session_key}\n"
            f"session id: {current_id[:8]}\n"
            f"消息数: {count}"
        )

    async def rename_session(self, name: str) -> str:
        old_key = self.session_key
        new_name = _clean_session_name(name)
        new_key = f"cli:{new_name}:local"
        if new_key == old_key:
            return f"会话已是: {new_key}"
        ok = await self.session_store.rename_session(old_key, new_key)
        if not ok:
            return f"无法重命名，会话不存在或目标已存在: {new_key}"
        assert self.agent_cache is not None
        agent = self.agent_cache.pop(old_key, None)
        if agent is not None:
            self.agent_cache[new_key] = agent
        self.session_name = new_name
        return f"会话已重命名: {old_key} -> {new_key}"

    async def delete_session(self, name: str | None = None) -> str:
        target_name = _clean_session_name(name) if name else self.session_name
        target_key = f"cli:{target_name}:local"
        if self.session_store.get(target_key) is None:
            return f"会话不存在: {target_key}"
        await self.session_store.delete_session(target_key)
        assert self.agent_cache is not None
        self.agent_cache.pop(target_key, None)
        if target_key == self.session_key:
            self.session_name = "default"
        await self.session_store.get_or_create(self.session_key, self.source)
        return f"会话已删除: {target_key}\n当前会话: {self.session_key}"

    async def get_agent(self):
        return await self.get_or_create_agent()

    async def load_history(self) -> list[dict]:
        session = await self.session_store.get_or_create(self.session_key, self.source)
        current_id = self.compression_chain.resolve(session.session_id)
        return await self.session_store.load_history(current_id)

    async def export_session(self) -> tuple[int, str]:
        session = await self.session_store.get_or_create(self.session_key, self.source)
        current_id = self.compression_chain.resolve(session.session_id)
        export_path = (
            self.settings.agent_data_dir
            / "exports"
            / f"{self.session_key.replace(':', '_')}.jsonl"
        )
        count = await self.session_store.export(current_id, str(export_path))
        return count, str(export_path)

    async def stop_agents(self) -> str:
        assert self.agent_cache is not None
        for agent in self.agent_cache.values():
            if hasattr(agent, "_interrupt_requested"):
                agent._interrupt_requested = True
        from personal_agent.tools.executor import set_interrupted
        from personal_agent.plugins.builtin.tools.builtin.delegate import stop_delegate_agents

        set_interrupted()
        stopped = stop_delegate_agents()
        if stopped:
            return f"已停止。已请求停止 {stopped} 个子 agent。"
        return "已停止。"

    def plugin_command_kwargs(self, args: str) -> dict:
        return {
            "args": args,
            "runtime": self,
            "session_key": self.session_key,
        }

    async def handle_command(self, text: str) -> str | None:
        from personal_agent.commands.runtime import handle_slash_command

        result = await handle_slash_command(self, text)
        if not result.handled:
            return None
        if result.continue_text is not None:
            return await self.run_message(result.continue_text)
        return result.response


async def create_cli_runtime(
    *,
    settings: Settings | None = None,
    session_name: str = "default",
) -> CliChatRuntime:
    app_runtime = await create_app_runtime(settings)

    return CliChatRuntime(
        settings=app_runtime.settings,
        plugin_manager=app_runtime.plugin_manager,
        db=app_runtime.db,
        session_store=app_runtime.session_store,
        compression_chain=app_runtime.compression_chain,
        memory_manager=app_runtime.memory_manager,
        mcp_manager=app_runtime.mcp_manager,
        app_runtime=app_runtime,
        session_name=_clean_session_name(session_name),
    )


async def run_cli_once(message: str, *, session_name: str = "default") -> str:
    runtime = await create_cli_runtime(session_name=session_name)
    try:
        return await runtime.run_once(message)
    finally:
        await runtime.close()


async def run_cli_repl(*, session_name: str = "default") -> None:
    runtime = await create_cli_runtime(session_name=session_name)
    try:
        await runtime.repl()
    finally:
        await runtime.close()


def run_cli_once_sync(message: str, *, session_name: str = "default") -> None:
    _configure_stdout()
    result = asyncio.run(run_cli_once(message, session_name=session_name))
    if result:
        print(result)


def run_cli_repl_sync(*, session_name: str = "default") -> None:
    _configure_stdout()
    asyncio.run(run_cli_repl(session_name=session_name))


def _configure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _clean_session_name(name: str) -> str:
    name = (name or "default").strip()
    return name.replace(":", "_") or "default"
