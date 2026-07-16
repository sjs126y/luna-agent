"""Gateway — central orchestrator: adapters, routing, session management, agent dispatch."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import OrderedDict
from pathlib import Path

from personal_agent.platforms.core import platform_registry
from personal_agent.commands.runtime import handle_slash_command
from personal_agent.conversation.input import ConversationInput
from personal_agent.conversation import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
    ConversationCommandRuntime,
    ConversationCoordinator,
    ConversationService,
    ResponseMode,
    SubmissionOrigin,
    SubmissionRequest,
    SessionDirectory,
)
from personal_agent.gateway.confirmations import PendingConfirmationManager
from personal_agent.gateway.session_router import GatewaySessionRouter
from personal_agent.gateway.session_store import SessionStore
from personal_agent.gateway.state import GatewayRunState, PlatformRuntime
from personal_agent.memory.review import MemoryReviewService
from personal_agent.delivery import DeliveryKind, DeliveryRequest
from personal_agent.models.messages import OutboundMessage

logger = logging.getLogger(__name__)


class Gateway:
    def __init__(
        self,
        config,
        db,
        memory_manager,
        system_prompt_template: str = "",
        plugin_manager=None,
        hook_manager=None,
        conversation_service: ConversationService | None = None,
        memory_review_service: MemoryReviewService | None = None,
        conversation_coordinator: ConversationCoordinator | None = None,
        session_directory: SessionDirectory | None = None,
        platform_directory=None,
        delivery_service=None,
        delivery_worker=None,
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
                hook_manager=hook_manager,
                session_store=session_store,
                compression_chain=compression_chain,
                memory_manager=memory_manager,
                memory_review_service=memory_review_service,
                system_prompt_template=system_prompt_template,
                agent_cache_max=128,
            )
        else:
            conversation_service.system_prompt_template = system_prompt_template
            if conversation_service.agent_cache_max is None:
                conversation_service.agent_cache_max = 128
        self._conversation_service = conversation_service
        self._conversation_coordinator = conversation_coordinator
        self._memory_review_service = memory_review_service or MemoryReviewService()
        self._compression_chain = conversation_service.compression_chain
        self._auth_manager = AuthManager(config, config.agent_data_dir)
        self._session_store = conversation_service.session_store
        self._adapters: list = []
        self._platforms: dict[str, PlatformRuntime] = {}
        self._platform_backoff_delays = tuple(getattr(config, "platform_reconnect_delays", (1, 2, 5, 10, 30, 60)))
        self._run_state = GatewayRunState()
        self._agent_cache: OrderedDict[str, object] = conversation_service.agent_cache
        self._session_router = session_directory or GatewaySessionRouter()
        self._session_override = self._session_router.overrides
        self._confirmations = PendingConfirmationManager()
        self._platform_directory = platform_directory
        self._delivery_service = delivery_service
        self._delivery_worker = delivery_worker
        self._cron_scheduler = None
        self.plugin_manager = plugin_manager
        self.hook_manager = hook_manager or getattr(plugin_manager, "hook_manager", None)
        self._shutdown_event = asyncio.Event()
        self._mcp_manager = None  # set by main.py after MCPManager.start()
        self._started = False

    # ── lifecycle ─────────────────────────────────────

    async def start(self) -> None:
        self._started = True
        self._shutdown_event = asyncio.Event()
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
            submission_port = self._conversation_coordinator or self
            self._cron_scheduler = CronScheduler(
                cron_store,
                submission_port,
                sessions=self._session_router,
            )
            self._cron_scheduler.start()
        else:
            self._cron_scheduler = None

        for entry in platform_registry.list():
            await self._start_platform(entry)
        if self._delivery_worker is not None:
            await self._delivery_worker.process_due()

        logger.info("Gateway started with %d platform(s)", len(self._adapters))
        await self._dispatch_gateway_observer(
            "GatewayStart",
            payload={"platform_count": len(self._adapters)},
        )

    async def stop(self) -> None:
        await self._dispatch_gateway_observer(
            "GatewayStop",
            payload={"platform_count": len(self._adapters)},
        )
        self._started = False
        if self._cron_scheduler:
            self._cron_scheduler.stop()
        mcp = getattr(self, '_mcp_manager', None)
        if mcp is not None:
            try:
                await mcp.stop()
            except Exception:
                logger.exception("Error stopping MCP manager")
        reconnect_tasks = [
            runtime.reconnect_task
            for runtime in self._platforms.values()
            if runtime.reconnect_task is not None and not runtime.reconnect_task.done()
        ]
        for task in reconnect_tasks:
            task.cancel()
        if reconnect_tasks:
            await asyncio.gather(*reconnect_tasks, return_exceptions=True)
        for adapter in self._adapters:
            try:
                await adapter.disconnect()
            except Exception:
                logger.exception("Error disconnecting adapter")
            finally:
                if hasattr(adapter, "mark_disconnected"):
                    adapter.mark_disconnected()
                if self._platform_directory is not None:
                    self._platform_directory.unregister(getattr(adapter, "_platform_name", ""))
                await self._dispatch_gateway_observer(
                    "PlatformDisconnected",
                    source=self._platform_source(getattr(adapter, "_platform_name", "")),
                    payload={"platform": getattr(adapter, "_platform_name", "")},
                )
        for runtime in self._platforms.values():
            runtime.mark_stopped()
        self._shutdown_event.set()

    async def wait_for_shutdown(self) -> None:
        await self._shutdown_event.wait()

    def health_snapshot(self) -> dict:
        steer_snapshot = self._conversation_service.steer_snapshot()
        platforms = [
            self._platforms[key].snapshot()
            for key in sorted(self._platforms)
        ]
        run_health = self._run_state.snapshot()
        for run in run_health.get("running_agent_runs", []):
            if not isinstance(run, dict):
                continue
            session_key = str(run.get("session_key") or "")
            session_steer = self._conversation_service.steer_snapshot(session_key)
            run["active_turn_id"] = session_steer.get("active_turn_id", "")
            run["pending_steers"] = int(session_steer.get("pending_count") or 0)
        data = {
            "started": self._started,
            "adapter_count": len(self._adapters),
            "platforms": platforms,
            "cached_agents": len(self._agent_cache),
            "cron_enabled": self._cron_scheduler is not None,
            "pending_messages": sum(int(item.get("pending_messages", 0)) for item in platforms),
            "active_adapter_sessions": sum(int(item.get("active_sessions", 0)) for item in platforms),
            "platform_reconnect_delays": list(self._platform_backoff_delays),
            "platform_pending_warning_threshold": getattr(self.config, "platform_pending_warning_threshold", 10),
            "platform_chat_locks_maxsize": getattr(self.config, "platform_chat_locks_maxsize", 64),
            "platform_message_dedupe_max_size": getattr(self.config, "platform_message_dedupe_max_size", 1024),
            "platform_send_max_retries": getattr(self.config, "platform_send_max_retries", 2),
            "steer": steer_snapshot,
            "pending_steer_count": int(steer_snapshot.get("pending_steer_count") or 0),
            "active_steer_sessions": list(steer_snapshot.get("active_steer_sessions") or []),
        }
        data.update(self._confirmations.snapshot() or {})
        data.update(run_health)
        return data

    async def _start_platform(self, entry) -> None:
        runtime = self._platforms.setdefault(
            entry.name,
            PlatformRuntime(name=entry.name, backoff_delays_seconds=self._platform_backoff_delays),
        )
        runtime.backoff_delays_seconds = self._platform_backoff_delays
        if not entry.check_fn(self.config):
            runtime.mark_skipped("check_fn returned False")
            logger.warning("Platform '%s' skipped: check_fn returned False", entry.name)
            return

        connected = await self._try_connect_platform(entry, runtime)
        if not connected:
            self._schedule_platform_reconnect(entry, runtime)

    async def _try_connect_platform(self, entry, runtime: PlatformRuntime) -> bool:
        runtime.mark_connecting()
        adapter = None
        try:
            adapter = entry.factory(self.config, self.db)
            adapter.set_message_handler(self._handle_message)
            if self._conversation_coordinator is not None and hasattr(adapter, "set_coordinator_managed"):
                adapter.set_coordinator_managed(True)
            if self._conversation_coordinator is None and hasattr(adapter, "set_outbound_handlers"):
                adapter.set_outbound_handlers(
                    before_send=self._before_platform_send,
                    after_send=self._after_platform_send,
                )
            if hasattr(adapter, "set_message_bypass_predicate"):
                adapter.set_message_bypass_predicate(self._should_bypass_adapter_queue)
            if hasattr(adapter, "set_attachment_store"):
                adapter.set_attachment_store(self._conversation_service.attachment_store)
            await adapter.connect()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if adapter is not None and hasattr(adapter, "mark_connect_error"):
                adapter.mark_connect_error(error, name=entry.name)
            runtime.mark_error(error, adapter)
            logger.exception("Platform '%s' connect failed", entry.name)
            return False

        if hasattr(adapter, "mark_connected"):
            adapter.mark_connected(name=entry.name)
        if adapter not in self._adapters:
            self._adapters.append(adapter)
        runtime.mark_connected(adapter)
        if self._platform_directory is not None:
            self._platform_directory.register(entry.name, adapter)
        logger.info("Platform '%s' connected", entry.name)
        await self._dispatch_gateway_observer(
            "PlatformConnected",
            source=self._platform_source(entry.name),
            payload={"platform": entry.name, "adapter": type(adapter).__name__},
        )
        return True

    def _schedule_platform_reconnect(self, entry, runtime: PlatformRuntime) -> None:
        if not self._started:
            return
        task = runtime.reconnect_task
        if task is not None and not task.done():
            return
        delay = runtime.next_retry_delay()
        runtime.mark_reconnecting(delay)
        runtime.reconnect_task = asyncio.create_task(
            self._reconnect_platform(entry, runtime),
            name=f"gateway-platform-reconnect:{entry.name}",
        )

    async def _reconnect_platform(self, entry, runtime: PlatformRuntime) -> None:
        try:
            while self._started:
                delay = runtime.next_retry_delay()
                runtime.mark_reconnecting(delay)
                await asyncio.sleep(delay)
                if not self._started:
                    return
                if not entry.check_fn(self.config):
                    runtime.mark_skipped("check_fn returned False")
                    return
                connected = await self._try_connect_platform(entry, runtime)
                if connected:
                    return
        except asyncio.CancelledError:
            raise
        finally:
            if runtime.reconnect_task is asyncio.current_task():
                runtime.reconnect_task = None

    # ── message handling ──────────────────────────────

    async def _handle_message(self, event) -> str | None:
        """Gateway callback from adapter. Returns response text."""
        from personal_agent.trace import trace_id, set_trace
        token = set_trace(f"{event.source.platform}:{event.source.user_id[:8]}")
        try:
            return await self._handle_message_inner(event)
        finally:
            trace_id.reset(token)

    def _should_bypass_adapter_queue(self, event) -> bool:
        session_key = self._session_router.active_key(event.source)
        if self._confirmations.get(session_key) is not None:
            return True
        if not self._run_state.is_running(session_key):
            return False
        text = str(getattr(event, "text", "") or "").strip()
        return text.startswith("/steer") or text.startswith("/stop")

    async def _handle_message_inner(self, event) -> str | None:
        if getattr(event, "envelope", None) is None and hasattr(event, "to_envelope"):
            event.to_envelope()
        session_key = self._session_router.active_key(event.source)

        # 1. Authorization (skip internal/cron events)
        if not event.internal and event.source.user_id != "cron":
            allowed, response = self._auth_manager.check(
                event.source.user_id, event.text
            )
            if not allowed:
                text = response or "抱歉，你没有权限使用此服务。"
                if self._conversation_coordinator is not None:
                    await self._deliver_gateway_text(session_key, text, DeliveryKind.AUTH)
                    return None
                return text
            # Auth passed with a message (e.g. pairing success greeting)
            if allowed and response is not None:
                if self._conversation_coordinator is not None:
                    await self._deliver_gateway_text(session_key, response, DeliveryKind.AUTH)
                    return None
                return response

        # 2. Formal inbound hooks run only after authorization.
        event, blocked_response = await self._dispatch_gateway_message(event, session_key)
        if blocked_response is not None:
            if self._conversation_coordinator is not None:
                await self._deliver_gateway_text(session_key, blocked_response, DeliveryKind.SYSTEM)
                return None
            return blocked_response

        # 3. Command detection. /stop must be able to cancel pending confirms.
        if self._conversation_coordinator is not None:
            consumed, confirm_response = self._confirmations.resolve_message(session_key, event.text)
            if consumed:
                if confirm_response:
                    await self._deliver_gateway_text(
                        session_key,
                        confirm_response,
                        DeliveryKind.SYSTEM,
                    )
                    return None
                return confirm_response
            event = await self._prepare_inbound_attachments(event)
            envelope = event.to_envelope() if hasattr(event, "to_envelope") else None
            user_input = (
                ConversationInput.from_envelope(envelope)
                if envelope is not None
                else ConversationInput.text_only(event.text, source=event.source)
            )
            request = SubmissionRequest(
                session_key=session_key,
                input=user_input,
                origin=SubmissionOrigin.GATEWAY,
                response_mode=ResponseMode.DELIVER,
                owner_id=str(event.source.user_id or ""),
                metadata={"message_id": str(getattr(event, "message_id", "") or "")},
                confirm=self._confirm_callback(event, session_key),
                command_runtime=_GatewayCommandRuntime(self, event, session_key),
            )
            handle = await self._conversation_coordinator.submit(request)
            await handle.outcome()
            return None

        if event.text.startswith("/"):
            cmd_result = await self._handle_command(event, session_key)
            if cmd_result is not None:
                return cmd_result
            # cmd_result is None → continue to agent (skill injection, etc.)

        # 3.5. Pending async tool confirmation replies bypass busy handling.
        consumed, confirm_response = self._confirmations.resolve_message(session_key, event.text)
        if consumed:
            return confirm_response

        # 4. Busy check
        if self._run_state.is_running(session_key):
            return "我正在处理你上一条消息，请稍候..."

        # 5. Mark running → process → cleanup
        self._run_state.begin(session_key, event.source)
        try:
            response = await self._handle_message_with_agent(event, session_key)
            self._run_state.complete(session_key)
            return response
        except Exception as exc:
            self._run_state.fail(session_key, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._run_state.end(session_key)

    # ── agent dispatch ────────────────────────────────

    async def _handle_message_with_agent(self, event, session_key: str) -> str:
        from personal_agent.conversation.input import ConversationInput

        event = await self._prepare_inbound_attachments(event)
        envelope = event.to_envelope() if hasattr(event, "to_envelope") else None
        if envelope is not None:
            turn = await _call_with_optional_confirm(
                self._conversation_service.run_turn_input,
                session_key,
                ConversationInput.from_envelope(envelope),
                confirm=self._confirm_callback(event, session_key),
            )
        else:
            turn = await _call_with_optional_confirm(
                self._conversation_service.run_turn,
                session_key,
                event.source,
                event.text,
                confirm=self._confirm_callback(event, session_key),
            )

        return turn.final_response or EMPTY_FINAL_RESPONSE_MESSAGE

    async def _dispatch_gateway_message(self, event, session_key: str):
        if self.hook_manager is None:
            return event, None
        from personal_agent.hooks import HookEvent

        envelope = event.to_envelope()
        outcome = await self.hook_manager.dispatch(self._gateway_hook_envelope(
            HookEvent.GATEWAY_MESSAGE_RECEIVED,
            source=event.source,
            session_key=session_key,
            payload={
                "text": event.text,
                "attachments": [item.as_dict() for item in envelope.attachments],
                "metadata": dict(envelope.metadata),
                "internal": bool(event.internal),
            },
        ))
        if outcome.blocked:
            return event, outcome.reason or None
        if outcome.text is not None:
            event.text = str(outcome.text)
            envelope.text = event.text
        if outcome.attachments is not None:
            envelope.attachments = list(outcome.attachments)
        if outcome.metadata is not None:
            envelope.metadata = dict(outcome.metadata)
        event.envelope = envelope
        return event, None

    async def _before_platform_send(self, source, text: str) -> str | None:
        if self.hook_manager is None:
            return text
        from personal_agent.hooks import HookEvent

        outcome = await self.hook_manager.dispatch(self._gateway_hook_envelope(
            HookEvent.GATEWAY_BEFORE_SEND,
            source=source,
            session_key=self._session_router.active_key(source),
            payload={"text": text},
        ))
        if outcome.suppressed:
            return None
        return outcome.text if outcome.text is not None else text

    async def _after_platform_send(
        self,
        source,
        text: str,
        success: bool,
        error: str,
    ) -> None:
        from personal_agent.hooks import HookEvent

        await self._dispatch_gateway_observer(
            HookEvent.GATEWAY_AFTER_SEND,
            source=source,
            session_key=self._session_router.active_key(source),
            payload={"text": text, "success": success, "error": error},
        )

    async def _dispatch_gateway_observer(
        self,
        event_name,
        *,
        source=None,
        session_key: str = "",
        payload: dict | None = None,
    ) -> None:
        if self.hook_manager is None:
            return
        from personal_agent.hooks import HookEvent

        event = event_name if isinstance(event_name, HookEvent) else HookEvent(str(event_name))
        await self.hook_manager.dispatch(self._gateway_hook_envelope(
            event,
            source=source,
            session_key=session_key,
            payload=payload or {},
        ))

    def _gateway_hook_envelope(
        self,
        event_name,
        *,
        source=None,
        session_key: str = "",
        payload: dict | None = None,
    ):
        from personal_agent.hooks import HookEnvelope, HookEvent, HookScope, HookSourceContext

        event = event_name if isinstance(event_name, HookEvent) else HookEvent(str(event_name))
        return HookEnvelope(
            event_name=event,
            scope=HookScope.RUNTIME if event in {HookEvent.GATEWAY_START, HookEvent.GATEWAY_STOP} else HookScope.SESSION,
            session_key=session_key,
            cwd=str(Path.cwd()),
            source=HookSourceContext(
                platform=str(getattr(source, "platform", "") or ""),
                user_id=str(getattr(source, "user_id", "") or ""),
                chat_id=str(getattr(source, "chat_id", "") or ""),
            ) if source is not None else None,
            payload=payload or {},
        )

    @staticmethod
    def _platform_source(platform: str):
        from personal_agent.models.messages import SessionSource

        return SessionSource(platform=str(platform or ""), user_id="gateway")

    def _confirm_callback(self, event, session_key: str):
        async def _confirm(decision):
            adapter = self._adapter_for_source(event.source)
            if adapter is None:
                return "deny"

            async def send(prompt: str) -> bool:
                if self._delivery_service is not None:
                    result = await self._delivery_service.deliver(DeliveryRequest(
                        session_key=session_key,
                        message=OutboundMessage.text(prompt),
                        kind=DeliveryKind.APPROVAL,
                    ))
                    return result.delivered
                try:
                    result = await adapter.send(event.source.chat_id, prompt)
                except Exception:
                    logger.exception("Failed to send tool confirmation prompt")
                    return False
                return bool(getattr(result, "success", False))

            return await self._confirmations.request(
                session_key=session_key,
                source=event.source,
                decision=decision,
                settings=self.config,
                send=send,
            )

        return _confirm

    async def _deliver_gateway_text(
        self,
        session_key: str,
        text: str,
        kind: DeliveryKind,
    ) -> bool:
        if self._delivery_service is None or not str(text or ""):
            return False
        result = await self._delivery_service.deliver(DeliveryRequest(
            session_key=session_key,
            message=OutboundMessage.text(text),
            kind=kind,
        ))
        return result.delivered

    async def _prepare_inbound_attachments(self, event):
        adapter = self._adapter_for_source(event.source)
        if adapter is None or not hasattr(adapter, "prepare_inbound_attachments"):
            return event
        try:
            return await adapter.prepare_inbound_attachments(event)
        except Exception:
            logger.exception("Inbound attachment preparation failed for platform %s", event.source.platform)
            return event

    def _adapter_for_source(self, source):
        platform = str(getattr(source, "platform", "") or "")
        runtime = self._platforms.get(platform)
        adapter = getattr(runtime, "adapter", None) if runtime is not None else None
        if adapter is not None:
            return adapter
        for candidate in self._adapters:
            if getattr(candidate, "_platform_name", "") == platform:
                return candidate
        return None

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

    def __init__(self, gateway: Gateway, event, session_key: str) -> None:
        self.gateway = gateway
        self.event = event
        self._session_key = session_key
        self.settings = gateway.config
        self.plugin_manager = gateway.plugin_manager
        self.conversation_service = gateway._conversation_service
        self.conversation_coordinator = gateway._conversation_coordinator

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

    async def pending_confirmation_status(self) -> dict | None:
        return self.gateway._confirmations.snapshot(self.session_key)

    async def is_session_running(self) -> bool:
        if self.conversation_coordinator is not None:
            return self.conversation_coordinator.active_turns.active_turn(self._session_key) is not None
        return self.gateway._run_state.is_running(self._session_key)

    def plugin_command_kwargs(self, args: str) -> dict:
        return {
            "event": self.event,
            "args": args,
            "gateway": self.gateway,
            "session_key": self._session_key,
        }

    async def stop_agents(self) -> str:
        self.gateway._confirmations.cancel(None)
        if self.conversation_coordinator is None:
            self.gateway._run_state.request_stop(self._session_key)
        return await super().stop_agents()

    def session_list_current_key(self) -> str:
        return self.gateway._session_router.current_for_list(self.event.source)


async def _call_with_optional_confirm(func, *args, confirm=None):
    try:
        signature = inspect.signature(func)
        accepts_confirm = (
            "confirm" in signature.parameters
            or any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
        )
    except (TypeError, ValueError):
        accepts_confirm = True
    if accepts_confirm:
        return await func(*args, confirm=confirm)
    return await func(*args)
