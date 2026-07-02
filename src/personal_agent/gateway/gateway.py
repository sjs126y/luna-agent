"""Gateway — central orchestrator: adapters, routing, session management, agent dispatch."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict

from personal_agent.adapters.base import platform_registry
from personal_agent.agent.hooks import Hooks
from personal_agent.commands.runtime import handle_slash_command
from personal_agent.conversation import ConversationCommandRuntime, ConversationService
from personal_agent.gateway.session_router import GatewaySessionRouter
from personal_agent.gateway.session_store import SessionStore
from personal_agent.memory.review import MemoryReviewService

logger = logging.getLogger(__name__)


class Gateway:
    def __init__(
        self,
        config,
        db,
        memory_manager,
        system_prompt_template: str = "",
        plugin_manager=None,
        conversation_service: ConversationService | None = None,
        memory_review_service: MemoryReviewService | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self._memory_manager = memory_manager
        self._system_prompt_template = system_prompt_template
        from personal_agent.gateway.compression_chain import CompressionChain
        from personal_agent.gateway.auth import AuthManager
        if conversation_service is None:
            compression_chain = CompressionChain(config.agent_data_dir / "compression_chain.json")
            session_store = SessionStore(db, config.agent_data_dir, chain=compression_chain)
            conversation_service = ConversationService(
                settings=config,
                plugin_manager=plugin_manager,
                session_store=session_store,
                compression_chain=compression_chain,
                memory_manager=memory_manager,
                system_prompt_template=system_prompt_template,
                agent_cache_max=128,
            )
        else:
            conversation_service.system_prompt_template = system_prompt_template
            if conversation_service.agent_cache_max is None:
                conversation_service.agent_cache_max = 128
        self._conversation_service = conversation_service
        self._memory_review_service = memory_review_service or MemoryReviewService()
        self._compression_chain = conversation_service.compression_chain
        self._auth_manager = AuthManager(config, config.agent_data_dir)
        self._session_store = conversation_service.session_store
        self._adapters: list = []
        self._platform_health: dict[str, dict] = {}
        self._running_agents: dict[str, bool] = {}
        self._agent_cache: OrderedDict[str, object] = conversation_service.agent_cache
        self._session_router = GatewaySessionRouter()
        self._session_override = self._session_router.overrides
        self._cron_scheduler = None
        self.hooks = Hooks()
        self.plugin_manager = plugin_manager
        self._shutdown_event = asyncio.Event()
        self._mcp_manager = None  # set by main.py after MCPManager.start()
        self._started = False

    # ── lifecycle ─────────────────────────────────────

    async def start(self) -> None:
        self._started = True
        self._compression_chain.load()
        self._session_router.overrides.update(self.config.session_override)
        await self._session_store.initialize()
        await self._session_store.expire_sessions(self.config.session_expire_days)

        if self.plugin_manager is not None:
            for plugin in self.plugin_manager.list_plugins():
                if plugin.enabled and plugin.manifest.kind == "platform":
                    self.plugin_manager.load_plugin(plugin.key)

        # Seed and start cron if enabled
        if self.config.enable_cron:
            from personal_agent.cron.store import CronStore
            from personal_agent.cron.scheduler import CronScheduler
            cron_store = CronStore(self.config.agent_data_dir / "cron" / "jobs.json")
            cron_store.seed_defaults()
            self._cron_scheduler = CronScheduler(cron_store, self)
            self._cron_scheduler.start()
        else:
            self._cron_scheduler = None

        for entry in platform_registry.list():
            if entry.check_fn(self.config):
                adapter = entry.factory(self.config, self.db)
                adapter.set_message_handler(self._handle_message)
                try:
                    await adapter.connect()
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    if hasattr(adapter, "mark_connect_error"):
                        adapter.mark_connect_error(error, name=entry.name)
                    self._platform_health[entry.name] = _platform_error_health(entry.name, adapter, error)
                    logger.exception("Platform '%s' connect failed", entry.name)
                    continue
                if hasattr(adapter, "mark_connected"):
                    adapter.mark_connected(name=entry.name)
                self._adapters.append(adapter)
                if hasattr(adapter, "health_snapshot"):
                    self._platform_health[entry.name] = adapter.health_snapshot()
                logger.info("Platform '%s' connected", entry.name)
            else:
                self._platform_health[entry.name] = {
                    "name": entry.name,
                    "adapter": "",
                    "connected": False,
                    "available": False,
                    "skipped_reason": "check_fn returned False",
                    "last_connect_error": "",
                    "last_send_error": "",
                    "active_sessions": 0,
                    "pending_messages": 0,
                    "pending_session_count": 0,
                }
                logger.warning("Platform '%s' skipped: check_fn returned False", entry.name)

        logger.info("Gateway started with %d platform(s)", len(self._adapters))

    async def stop(self) -> None:
        if self._cron_scheduler:
            self._cron_scheduler.stop()
        mcp = getattr(self, '_mcp_manager', None)
        if mcp is not None:
            try:
                await mcp.stop()
            except Exception:
                logger.exception("Error stopping MCP manager")
        for adapter in self._adapters:
            try:
                await adapter.disconnect()
            except Exception:
                logger.exception("Error disconnecting adapter")
            finally:
                if hasattr(adapter, "mark_disconnected"):
                    adapter.mark_disconnected()
        self._shutdown_event.set()
        self._started = False

    async def wait_for_shutdown(self) -> None:
        await self._shutdown_event.wait()

    def health_snapshot(self) -> dict:
        platform_health = dict(self._platform_health)
        for adapter in self._adapters:
            if hasattr(adapter, "health_snapshot"):
                data = adapter.health_snapshot()
                platform_health[str(data.get("name") or type(adapter).__name__)] = data
        platforms = [platform_health[key] for key in sorted(platform_health)]
        return {
            "started": self._started,
            "adapter_count": len(self._adapters),
            "platforms": platforms,
            "running_agents": len(self._running_agents),
            "running_agent_sessions": sorted(self._running_agents),
            "cached_agents": len(self._agent_cache),
            "cron_enabled": self._cron_scheduler is not None,
            "pending_messages": sum(int(item.get("pending_messages", 0)) for item in platforms),
            "active_adapter_sessions": sum(int(item.get("active_sessions", 0)) for item in platforms),
        }

    # ── message handling ──────────────────────────────

    async def _handle_message(self, event) -> str | None:
        """Gateway callback from adapter. Returns response text."""
        from personal_agent.trace import trace_id, set_trace
        token = set_trace(f"{event.source.platform}:{event.source.user_id[:8]}")
        try:
            return await self._handle_message_inner(event)
        finally:
            trace_id.reset(token)

    async def _handle_message_inner(self, event) -> str | None:
        session_key = self._session_router.active_key(event.source)

        # 1. Hook: on_message_received (only if hooks registered)
        if self.hooks.on_message_received:
            hook_result = await self.hooks.fire("on_message_received", event)
            if hook_result is None:
                return None  # dropped
            if hook_result is not event:
                event = hook_result
        if self.plugin_manager is not None:
            hook_result = await self.plugin_manager.invoke_hook("on_message_received", event)
            if hook_result is not None and hook_result is not event:
                event = hook_result

        # 2. Authorization (skip internal/cron events)
        if not event.internal and event.source.user_id != "cron":
            allowed, response = self._auth_manager.check(
                event.source.user_id, event.text
            )
            if not allowed:
                return response or "抱歉，你没有权限使用此服务。"
            # Auth passed with a message (e.g. pairing success greeting)
            if allowed and response is not None:
                return response

        # 3. Command detection
        if event.text.startswith("/"):
            cmd_result = await self._handle_command(event, session_key)
            if cmd_result is not None:
                return cmd_result
            # cmd_result is None → continue to agent (skill injection, etc.)

        # 4. Busy check
        if session_key in self._running_agents:
            return "我正在处理你上一条消息，请稍候..."

        # 5. Mark running → process → cleanup
        self._running_agents[session_key] = True
        try:
            return await self._handle_message_with_agent(event, session_key)
        finally:
            self._running_agents.pop(session_key, None)

    # ── agent dispatch ────────────────────────────────

    async def _handle_message_with_agent(self, event, session_key: str) -> str:
        turn = await self._conversation_service.run_turn(session_key, event.source, event.text)

        # Hook: on_before_send
        final = turn.final_response
        hook_result = await self.hooks.fire("on_before_send", final, event.source)
        if isinstance(hook_result, str):
            final = hook_result
        if self.plugin_manager is not None:
            hook_result = await self.plugin_manager.invoke_hook("on_before_send", final, event.source)
            if isinstance(hook_result, str):
                final = hook_result

        # Background memory review (Hermes-style nudge)
        agent = self._conversation_service.get_cached_agent(session_key)
        self._memory_review_service.maybe_spawn(
            agent=agent,
            messages=turn.messages,
            should_review=turn.should_review_memory,
            final_response=final,
        )

        return final or "..."

    async def _get_or_create_agent(self, session_key: str):
        """Return cached Agent if available, otherwise create and cache."""
        return await self._conversation_service.get_or_create_agent(session_key)


    # ── commands ──────────────────────────────────────

    async def _handle_command(self, event, session_key: str) -> str | None:
        runtime = _GatewayCommandRuntime(self, event, session_key)
        result = await handle_slash_command(runtime, event.text)
        if not result.handled:
            return None
        if result.continue_text is not None:
            event.text = result.continue_text
            return None
        return result.response

    # ── auth ──────────────────────────────────────────
    # Auth is now handled by AuthManager — see gateway/auth.py


class _GatewayCommandRuntime(ConversationCommandRuntime):
    reset_session_response = "会话已重置。开始新的对话吧。（历史对话保留，可用 /session 查看）"
    usage_create_agent = False
    allow_all_cached_agents = True

    def __init__(self, gateway: Gateway, event, session_key: str) -> None:
        self.gateway = gateway
        self.event = event
        self._session_key = session_key
        self.settings = gateway.config
        self.plugin_manager = gateway.plugin_manager
        self.conversation_service = gateway._conversation_service

    @property
    def session_key(self) -> str:
        return self._session_key

    @property
    def plugin_command_scopes(self) -> tuple[str]:
        return ("slash",)

    @property
    def source(self):
        return self.event.source

    async def switch_session(self, name: str) -> str:
        new_key = self.gateway._session_router.switch(self.event.source, name)
        await self.gateway._conversation_service.ensure_session(new_key, self.event.source)
        self._session_key = new_key
        return f"会话已切换: {new_key}"

    async def rename_session(self, name: str) -> str:
        old_key = self._session_key
        new_key = self.gateway._session_router.named_key(self.event.source, name)
        if new_key == old_key:
            return f"会话已是: {new_key}"
        ok = await self.gateway._conversation_service.rename_session(old_key, new_key)
        if not ok:
            return f"无法重命名，会话不存在或目标已存在: {new_key}"
        self.gateway._session_router.rename(self.event.source, old_key, new_key)
        self._session_key = new_key
        return f"会话已重命名: {old_key} -> {new_key}"

    async def delete_session(self, name: str | None = None) -> str:
        target_key = (
            self._session_key
            if name is None
            else self.gateway._session_router.named_key(self.event.source, name)
        )
        if self.gateway._session_store.get(target_key) is None:
            return f"会话不存在: {target_key}"
        await self.gateway._conversation_service.delete_session(target_key)
        base_key = self.gateway._session_router.delete(self.event.source, target_key)
        if target_key == self._session_key:
            self._session_key = base_key
            await self.gateway._conversation_service.ensure_session(base_key, self.event.source)
        return f"会话已删除: {target_key}\n当前会话: {self._session_key}"

    def plugin_command_kwargs(self, args: str) -> dict:
        return {
            "event": self.event,
            "args": args,
            "gateway": self.gateway,
            "session_key": self._session_key,
        }

    def session_list_current_key(self) -> str:
        return self.gateway._session_router.current_for_list(self.event.source)


def _platform_error_health(name: str, adapter, error: str) -> dict:
    data = adapter.health_snapshot() if hasattr(adapter, "health_snapshot") else {}
    data.update({
        "name": name,
        "adapter": type(adapter).__name__,
        "connected": False,
        "available": True,
        "last_connect_error": error,
    })
    data.setdefault("last_send_error", "")
    data.setdefault("active_sessions", 0)
    data.setdefault("pending_messages", 0)
    data.setdefault("pending_session_count", 0)
    return data
