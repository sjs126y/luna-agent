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

    async def close(self) -> None:
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
        output_fn("Personal Agent CLI。输入 exit/quit 或空行退出，/help 查看命令。")
        while True:
            try:
                text = input_fn(">>> ")
            except (EOFError, KeyboardInterrupt):
                output_fn("")
                break
            text = text.strip()
            if not text or text.lower() in {"exit", "quit"}:
                break
            response = await self.run_once(text)
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

        set_interrupted()
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
    settings = settings or Settings()
    settings.agent_data_dir.mkdir(parents=True, exist_ok=True)

    from personal_agent.main import _ensure_system_files
    from personal_agent.plugins.manager import PluginManager
    from personal_agent.tools.audit import set_audit_path
    from personal_agent.tools.sandbox import init_sandbox

    plugin_manager = PluginManager(settings)
    plugin_manager.discover()
    plugin_manager.load_enabled()
    await plugin_manager.invoke_hook("configure", settings=settings)

    init_sandbox(settings.sandbox_roots, settings.sandbox_blocked)
    if settings.audit_enabled:
        set_audit_path(settings.agent_data_dir / "audit.log")

    mcp_manager = await _start_mcp_manager(settings, plugin_manager)

    db = Database(settings.agent_data_dir / "state.db")
    await db.initialize()

    compression_chain = CompressionChain(settings.agent_data_dir / "compression_chain.json")
    compression_chain.load()
    session_store = SessionStore(db, settings.agent_data_dir, chain=compression_chain)
    await session_store.initialize()
    await session_store.expire_sessions(settings.session_expire_days)

    system_dir = settings.agent_data_dir / "system"
    _ensure_system_files(system_dir)
    builtin_memory = await plugin_manager.invoke_hook(
        "create_builtin_memory_provider",
        system_dir=system_dir,
    )
    if builtin_memory is None:
        raise RuntimeError("No built-in memory provider registered")

    external_memory = None
    if settings.memory_external_provider == "embedding":
        external_memory = await plugin_manager.invoke_hook(
            "create_external_memory_provider",
            settings=settings,
            data_dir=settings.agent_data_dir / "memory",
        )
        if external_memory is not None:
            logger.info("External memory: embedding (BAAI/bge-small-zh-v1.5)")

    return CliChatRuntime(
        settings=settings,
        plugin_manager=plugin_manager,
        db=db,
        session_store=session_store,
        compression_chain=compression_chain,
        memory_manager=MemoryManager(builtin=builtin_memory, external=external_memory),
        mcp_manager=mcp_manager,
        session_name=_clean_session_name(session_name),
    )


async def _start_mcp_manager(settings, plugin_manager):
    mcp_servers = list(settings.mcp_servers)
    for cfg in plugin_manager.get_mcp_servers():
        if isinstance(cfg, dict):
            mcp_servers.append(cfg)
        else:
            mcp_servers.append({
                "name": getattr(cfg, "name", ""),
                "command": getattr(cfg, "command", ""),
                "args": getattr(cfg, "args", []),
                "env": getattr(cfg, "env", {}),
                "enabled": getattr(cfg, "enabled", True),
            })
    if not settings.mcp_enabled or not mcp_servers:
        return None

    from personal_agent.mcp.manager import MCPManager

    manager = MCPManager(mcp_servers)
    await manager.start()
    return manager


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
