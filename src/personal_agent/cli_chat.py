"""Interactive persistent CLI chat runtime."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

from personal_agent.config import Settings
from personal_agent.db.database import Database
from personal_agent.gateway.compression_chain import CompressionChain
from personal_agent.gateway.session_store import SessionStore
from personal_agent.memory.manager import MemoryManager
from personal_agent.models.messages import SessionSource
from personal_agent.conversation import ConversationCommandRuntime, ConversationService
from personal_agent.runtime import AppRuntime, create_app_runtime

CLI_SYSTEM_PROMPT = (
    "你是一个智能助手。优先使用工具获取实时信息和执行操作，不要凭记忆编造。用中文回复。"
)


@dataclass
class CliChatRuntime(ConversationCommandRuntime):
    settings: Settings
    plugin_manager: object
    db: Database
    session_store: SessionStore
    compression_chain: CompressionChain
    memory_manager: MemoryManager
    mcp_manager: object | None = None
    app_runtime: AppRuntime | None = None
    conversation_service: ConversationService | None = None
    system_prompt_template: str = CLI_SYSTEM_PROMPT
    session_name: str = "default"
    agent_cache: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.conversation_service is None:
            self.conversation_service = ConversationService(
                settings=self.settings,
                plugin_manager=self.plugin_manager,
                session_store=self.session_store,
                compression_chain=self.compression_chain,
                memory_manager=self.memory_manager,
                system_prompt_template=self.system_prompt_template,
                agent_cache=self.agent_cache,
            )
        else:
            self.conversation_service.system_prompt_template = self.system_prompt_template
        self.agent_cache = self.conversation_service.agent_cache

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

    async def run_message(self, text: str) -> str:
        assert self.conversation_service is not None
        result = await self.conversation_service.run_turn(self.session_key, self.source, text)
        return result.final_response or "..."

    async def run_message_events(self, text: str, *, event_sink=None, confirm=None):
        assert self.conversation_service is not None
        return await self.conversation_service.run_turn_events(
            self.session_key,
            self.source,
            text,
            event_sink=event_sink,
            confirm=confirm,
        )

    async def switch_session(self, name: str) -> str:
        self.session_name = _clean_session_name(name)
        assert self.conversation_service is not None
        await self.conversation_service.ensure_session(self.session_key, self.source)
        return f"会话已切换: {self.session_key}"

    async def rename_session(self, name: str) -> str:
        old_key = self.session_key
        new_name = _clean_session_name(name)
        new_key = f"cli:{new_name}:local"
        if new_key == old_key:
            return f"会话已是: {new_key}"
        assert self.conversation_service is not None
        ok = await self.conversation_service.rename_session(old_key, new_key)
        if not ok:
            return f"无法重命名，会话不存在或目标已存在: {new_key}"
        self.session_name = new_name
        return f"会话已重命名: {old_key} -> {new_key}"

    async def delete_session(self, name: str | None = None) -> str:
        target_name = _clean_session_name(name) if name else self.session_name
        target_key = f"cli:{target_name}:local"
        assert self.conversation_service is not None
        if self.session_store.get(target_key) is None:
            return f"会话不存在: {target_key}"
        await self.conversation_service.delete_session(target_key)
        if target_key == self.session_key:
            self.session_name = "default"
        await self.conversation_service.ensure_session(self.session_key, self.source)
        return f"会话已删除: {target_key}\n当前会话: {self.session_key}"

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
        conversation_service=app_runtime.conversation_service,
        session_name=_clean_session_name(session_name),
    )


async def run_cli_once(message: str, *, session_name: str = "default") -> str:
    runtime = await create_cli_runtime(session_name=session_name)
    try:
        return await runtime.run_once(message)
    finally:
        await runtime.close()


def run_cli_once_sync(message: str, *, session_name: str = "default") -> None:
    _configure_stdout()
    result = asyncio.run(run_cli_once(message, session_name=session_name))
    if result:
        print(result)


def _configure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _clean_session_name(name: str) -> str:
    name = (name or "default").strip()
    return name.replace(":", "_") or "default"
