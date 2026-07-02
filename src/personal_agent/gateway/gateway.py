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
        self._compression_chain = conversation_service.compression_chain
        self._auth_manager = AuthManager(config, config.agent_data_dir)
        self._session_store = conversation_service.session_store
        self._adapters: list = []
        self._running_agents: dict[str, bool] = {}
        self._agent_cache: OrderedDict[str, object] = conversation_service.agent_cache
        self._session_router = GatewaySessionRouter()
        self._session_override = self._session_router.overrides
        self._cron_scheduler = None
        self.hooks = Hooks()
        self.plugin_manager = plugin_manager
        self._shutdown_event = asyncio.Event()
        self._mcp_manager = None  # set by main.py after MCPManager.start()

    # ── lifecycle ─────────────────────────────────────

    async def start(self) -> None:
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
                except Exception:
                    logger.exception("Platform '%s' connect failed", entry.name)
                    continue
                self._adapters.append(adapter)
                logger.info("Platform '%s' connected", entry.name)
            else:
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
        self._shutdown_event.set()

    async def wait_for_shutdown(self) -> None:
        await self._shutdown_event.wait()

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
        if turn.should_review_memory and final and agent is not None:
            self._spawn_memory_review(agent, turn.messages)

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

    # ── memory review ────────────────────────────────

    _MEMORY_REVIEW_PROMPT = (
        "Review this conversation and save anything worth remembering.\n\n"
        "Focus on:\n"
        "1. Has the user revealed personal details, preferences, or facts worth keeping?\n"
        "2. Has the user expressed expectations about how you should behave?\n\n"
        "If something stands out, call the memory tool to save it. "
        "Use target='user' for preferences, target='memory' for facts.\n"
        "If nothing is worth saving, just reply 'Nothing to save.' and stop."
    )

    def _spawn_memory_review(self, agent, messages: list[dict]) -> None:
        """Spawn a lightweight background review to extract memories."""
        import threading

        def _run():
            import asyncio as _asyncio
            _asyncio.run(self._do_memory_review(agent, list(messages)))

        t = threading.Thread(target=_run, daemon=True, name="mem-review")
        t.start()
        logger.debug("Memory review spawned")

    async def _do_memory_review(self, agent, messages: list[dict]) -> None:
        """Run a quick LLM call to review conversation and save memories."""
        try:
            review_messages = list(messages[-12:])  # last 12 messages only
            review_messages.append({
                "role": "user",
                "content": [{"type": "text", "text": self._MEMORY_REVIEW_PROMPT}],
            })
            response = await agent._transport.call(
                messages=review_messages,
                system_prompt="你是一个记忆管理助手。判断对话中是否有值得保存的信息。",
                tools=agent.tools,
                max_tokens=512,
            )
            if response.tool_calls:
                from personal_agent.tools.executor import execute_tool_calls
                await execute_tool_calls(response.tool_calls, review_messages, agent=agent)
                logger.info("Memory review: %d memories saved", len(response.tool_calls))
        except Exception:
            pass  # best-effort, never block the turn

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
