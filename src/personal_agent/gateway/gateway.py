"""Gateway — central orchestrator: adapters, routing, session management, agent dispatch."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict

from personal_agent.adapters.base import platform_registry
from personal_agent.agent.hooks import Hooks
from personal_agent.commands.runtime import handle_slash_command
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
    ) -> None:
        self.config = config
        self.db = db
        self._memory_manager = memory_manager
        self._system_prompt_template = system_prompt_template
        from personal_agent.gateway.compression_chain import CompressionChain
        from personal_agent.gateway.auth import AuthManager
        self._compression_chain = CompressionChain(config.agent_data_dir / "compression_chain.json")
        self._auth_manager = AuthManager(config, config.agent_data_dir)
        self._session_store = SessionStore(db, config.agent_data_dir, chain=self._compression_chain)
        self._adapters: list = []
        self._running_agents: dict[str, bool] = {}
        self._agent_cache: OrderedDict[str, object] = OrderedDict()
        self._session_override: dict[str, str] = {}  # platform:user → custom chat_id
        self._cron_scheduler = None
        self.hooks = Hooks()
        self.plugin_manager = plugin_manager
        self._shutdown_event = asyncio.Event()
        self._mcp_manager = None  # set by main.py after MCPManager.start()

    # ── lifecycle ─────────────────────────────────────

    async def start(self) -> None:
        self._compression_chain.load()
        self._session_override.update(self.config.session_override)  # config defaults
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
        session_key = f"{event.source.platform}:{event.source.chat_id}:{event.source.user_id}"
        # Apply session override if set (via /session command)
        override = self._session_override.get(session_key)
        if override:
            session_key = override

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
            if self.plugin_manager is not None:
                await self.plugin_manager.invoke_hook("on_session_selected", session_key=session_key)
            return await self._handle_message_with_agent(event, session_key)
        finally:
            self._running_agents.pop(session_key, None)

    # ── agent dispatch ────────────────────────────────

    async def _handle_message_with_agent(self, event, session_key: str) -> str:
        session = await self._session_store.get_or_create(session_key, event.source)

        # Walk chain to find the latest (uncompressed) session
        current_id = self._compression_chain.resolve(session.session_id)
        history = await self._session_store.load_history(current_id)
        previous_count = len(history)

        agent = await self._get_or_create_agent(session_key)

        from personal_agent.agent.context import build_turn_context
        from personal_agent.agent.loop import run_conversation

        ctx = await build_turn_context(agent, event.text, history)
        result = await run_conversation(agent, ctx)

        # Only save transcript if the conversation completed cleanly.
        # On error (completed=False), messages may be corrupted (orphan tool_use).
        if result.get("completed") and not result.get("context_overflow"):
            target_session_id = current_id
            if ctx.was_compressed:
                target_session_id = await self._session_store.create_compressed_session(
                    session_key, event.source, result["messages"]
                )
            else:
                await self._session_store.save_transcript(
                    target_session_id, result["messages"], previous_count
                )

        # Hook: on_before_send
        final = result.get("final_response", "")
        hook_result = await self.hooks.fire("on_before_send", final, event.source)
        if isinstance(hook_result, str):
            final = hook_result
        if self.plugin_manager is not None:
            hook_result = await self.plugin_manager.invoke_hook("on_before_send", final, event.source)
            if isinstance(hook_result, str):
                final = hook_result

        # Background memory review (Hermes-style nudge)
        if result.get("should_review_memory") and final:
            self._spawn_memory_review(agent, ctx.messages)

        return final or "..."

    async def _get_or_create_agent(self, session_key: str):
        """Return cached Agent if available, otherwise create and cache."""
        if session_key in self._agent_cache:
            agent = self._agent_cache[session_key]
            # Check if tools stale (registry generation changed)
            from personal_agent.tools.registry import tool_registry
            if agent._tools_generation == tool_registry.generation:
                return agent
            # Tools changed — evict stale cache entry
            del self._agent_cache[session_key]

        return await self._create_agent(session_key)

    async def _create_agent(self, session_key: str):
        from personal_agent.agent.factory import create_agent_runtime

        runtime = await create_agent_runtime(
            self.config,
            memory_manager=self._memory_manager,
            plugin_manager=self.plugin_manager,
            system_prompt_template=self._system_prompt_template,
        )
        agent = runtime.agent

        # LRU eviction if cache too large
        if len(self._agent_cache) >= 128:
            self._agent_cache.popitem(last=False)
        self._agent_cache[session_key] = agent
        return agent


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


class _GatewayCommandRuntime:
    def __init__(self, gateway: Gateway, event, session_key: str) -> None:
        self.gateway = gateway
        self.event = event
        self._session_key = session_key
        self.settings = gateway.config
        self.plugin_manager = gateway.plugin_manager

    @property
    def session_key(self) -> str:
        return self._session_key

    @property
    def source(self):
        return self.event.source

    async def get_agent(self):
        return await self.gateway._get_or_create_agent(self._session_key)

    async def reset_session(self) -> str:
        await self.gateway._session_store.reset_session(self._session_key, self.event.source)
        return "会话已重置。开始新的对话吧。（历史对话保留，可用 /session 查看）"

    async def clear_agent(self) -> None:
        self.gateway._agent_cache.pop(self._session_key, None)

    async def switch_session(self, name: str) -> str:
        base_key = self._base_key()
        platform, user_id = self._platform_user()
        new_key = f"{platform}:{name}:{user_id}"
        self.gateway._session_override[base_key] = new_key
        await self.gateway._session_store.get_or_create(new_key, self.event.source)
        self._session_key = new_key
        return f"会话已切换: {new_key}"

    async def list_sessions(self) -> str:
        base_key = self._base_key()
        current = self.gateway._session_override.get(base_key, base_key)
        platform, user_id = self._platform_user()
        sessions = await self.gateway._session_store.list_user_sessions(platform, user_id)
        lines = [f"当前会话: {current}", "你的会话列表:"]
        for item in sessions[:10]:
            marker = " <-" if item["session_key"] == current else ""
            lines.append(f"  {item['session_key']}{marker} ({item.get('message_count', 0)} 条消息)")
        if len(lines) == 2:
            lines.append("  无")
        return "\n".join(lines)

    async def current_session(self) -> str:
        session = await self.gateway._session_store.get_or_create(self._session_key, self.event.source)
        current_id = self.gateway._compression_chain.resolve(session.session_id)
        count = len(await self.gateway._session_store.load_history(current_id))
        return (
            f"当前会话: {self._session_key}\n"
            f"session id: {current_id[:8]}\n"
            f"消息数: {count}"
        )

    async def rename_session(self, name: str) -> str:
        old_key = self._session_key
        platform, user_id = self._platform_user()
        new_key = f"{platform}:{name}:{user_id}"
        if new_key == old_key:
            return f"会话已是: {new_key}"
        ok = await self.gateway._session_store.rename_session(old_key, new_key)
        if not ok:
            return f"无法重命名，会话不存在或目标已存在: {new_key}"
        agent = self.gateway._agent_cache.pop(old_key, None)
        if agent is not None:
            self.gateway._agent_cache[new_key] = agent
        base_key = self._base_key()
        if old_key == base_key:
            self.gateway._session_override[base_key] = new_key
        else:
            for key, value in list(self.gateway._session_override.items()):
                if value == old_key:
                    self.gateway._session_override[key] = new_key
        self._session_key = new_key
        return f"会话已重命名: {old_key} -> {new_key}"

    async def delete_session(self, name: str | None = None) -> str:
        base_key = self._base_key()
        platform, user_id = self._platform_user()
        target_key = self._session_key if name is None else f"{platform}:{name}:{user_id}"
        if self.gateway._session_store.get(target_key) is None:
            return f"会话不存在: {target_key}"
        await self.gateway._session_store.delete_session(target_key)
        self.gateway._agent_cache.pop(target_key, None)
        for key, value in list(self.gateway._session_override.items()):
            if key == target_key or value == target_key:
                del self.gateway._session_override[key]
        if target_key == self._session_key:
            self._session_key = base_key
            await self.gateway._session_store.get_or_create(base_key, self.event.source)
        return f"会话已删除: {target_key}\n当前会话: {self._session_key}"

    async def load_history(self) -> list[dict]:
        session = await self.gateway._session_store.get_or_create(self._session_key, self.event.source)
        current_id = self.gateway._compression_chain.resolve(session.session_id)
        return await self.gateway._session_store.load_history(current_id)

    async def export_session(self) -> tuple[int, str]:
        session = await self.gateway._session_store.get_or_create(self._session_key, self.event.source)
        current_id = self.gateway._compression_chain.resolve(session.session_id)
        export_path = (
            self.gateway.config.agent_data_dir
            / "exports"
            / f"{self._session_key.replace(':', '_')}.jsonl"
        )
        count = await self.gateway._session_store.export(current_id, str(export_path))
        return count, str(export_path)

    async def usage(self, *, current_user_message: str = "") -> str:
        agent = self.gateway._agent_cache.get(self._session_key)
        if agent is None:
            return "暂无会话数据。"
        history = await self.load_history()

        from personal_agent.context_budget import build_context_budget

        budget = await build_context_budget(
            messages=history,
            agent=agent,
            settings=self.gateway.config,
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

    async def allow_category(self, category: str) -> str:
        for agent in self.gateway._agent_cache.values():
            if hasattr(agent, "_destructive_allowed"):
                agent._destructive_allowed.add(category)
        return f"已授权 {category} 操作，本轮对话内有效。"

    async def stop_agents(self) -> str:
        for agent in self.gateway._agent_cache.values():
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
            "event": self.event,
            "args": args,
            "gateway": self.gateway,
            "session_key": self._session_key,
        }

    def _base_key(self) -> str:
        return f"{self.event.source.platform}:{self.event.source.chat_id}:{self.event.source.user_id}"

    def _platform_user(self) -> tuple[str, str]:
        parts = self._base_key().split(":", 2)
        platform = parts[0]
        user_id = parts[2] if len(parts) > 2 else ""
        return platform, user_id
