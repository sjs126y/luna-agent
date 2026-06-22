"""Feishu (Lark) adapter — WebSocket long connection via lark-oapi."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from personal_agent.adapters.base import BasePlatformAdapter, ChatInfo, SendResult
from personal_agent.models.messages import MessageEvent, SessionSource

logger = logging.getLogger(__name__)


class FeishuAdapter(BasePlatformAdapter):
    def __init__(self, config, db) -> None:
        super().__init__(config, db)
        self._ws_client = None
        self._lark_client = None  # Reused API client
        self._app_id = config.feishu_app_id
        self._app_secret = config.feishu_app_secret

    # ── connect / disconnect ──────────────────────────

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        logger.info("Feishu adapter connecting (app_id=%s...)", self._app_id[:8])

        # Create reusable API client (used by send / get_chat_info)
        import lark_oapi as lark
        self._lark_client = lark.Client.builder() \
            .app_id(self._app_id) \
            .app_secret(self._app_secret) \
            .build()

        # Event to signal WS thread status
        import threading
        self._stop_event = threading.Event()
        self._ws_ready = threading.Event()

        def _run_ws():
            # WS client module captures the event loop at import time.
            # Give it a fresh event loop for this daemon thread (avoids
            # "event loop already running" / cross-thread event loop errors).
            import lark_oapi.ws.client as ws_client_module
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            ws_client_module.loop = new_loop

            from lark_oapi.ws import Client as WsClient
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandlerBuilder

            def on_message(event_data):
                logger.debug("Feishu WS raw event received: type=%s", type(event_data).__name__)
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._handle_feishu_event(event_data), self._loop
                    )
                except Exception:
                    logger.exception("Feishu WS run_coroutine_threadsafe failed")

            handler = EventDispatcherHandlerBuilder("", "") \
                .register_p2_im_message_receive_v1(on_message) \
                .build()

            try:
                client = WsClient(
                    app_id=self._app_id,
                    app_secret=self._app_secret,
                    event_handler=handler,
                )
                self._ws_client = client
                self._ws_ready.set()
                logger.info("Feishu WS client starting (v2 SDK)")
                client.start()
            except Exception:
                logger.exception("Feishu WS client failed to start")
                self._ws_ready.set()

        self._ws_thread = threading.Thread(target=_run_ws, daemon=True, name="feishu-ws")
        self._ws_thread.start()

        if not self._ws_ready.wait(timeout=10):
            logger.warning("Feishu WS connection timed out after 10s")
        else:
            logger.info("Feishu adapter connected")

    async def disconnect(self) -> None:
        if self._ws_client:
            try:
                self._ws_client.stop()
            except Exception:
                pass
            self._ws_client = None
        self._lark_client = None
        if hasattr(self, '_stop_event'):
            self._stop_event.set()
        logger.info("Feishu adapter disconnected")

    # ── send ──────────────────────────────────────────

    async def send(self, chat_id: str, content: str) -> SendResult:
        try:
            import lark_oapi as lark

            if chat_id.startswith("oc_"):
                req = lark.im.v1.CreateMessageRequest.builder() \
                    .receive_id_type("chat_id") \
                    .request_body(
                        lark.im.v1.CreateMessageRequestBody.builder()
                        .receive_id(chat_id)
                        .msg_type("text")
                        .content(json.dumps({"text": content}))
                        .build()
                    ).build()
            else:
                req = lark.im.v1.CreateMessageRequest.builder() \
                    .receive_id_type("open_id") \
                    .request_body(
                        lark.im.v1.CreateMessageRequestBody.builder()
                        .receive_id(chat_id)
                        .msg_type("text")
                        .content(json.dumps({"text": content}))
                        .build()
                    ).build()

            resp = self._lark_client.im.v1.message.create(req)
            if resp.success():
                return SendResult(success=True, message_id=resp.data.message_id)
            return SendResult(success=False, error=f"Feishu API error: {resp.code} {resp.msg}")
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    # ── get_chat_info ─────────────────────────────────

    async def get_chat_info(self, chat_id: str) -> ChatInfo:
        try:
            if chat_id.startswith("oc_"):
                import lark_oapi as lark
                req = lark.im.v1.GetChatRequest.builder().chat_id(chat_id).build()
                resp = self._lark_client.im.v1.chat.get(req)
                if resp.success():
                    return ChatInfo(
                        chat_id=chat_id,
                        chat_type="group" if resp.data.chat_type == "group" else "dm",
                        chat_name=resp.data.name or "",
                        member_count=resp.data.member_count or 0,
                    )
        except Exception:
            logger.exception("get_chat_info failed")
        return ChatInfo(chat_id=chat_id, chat_type="dm")

    # ── message parsing ───────────────────────────────

    async def _handle_feishu_event(self, event_data) -> None:
        """Parse Feishu v2 event (P2ImMessageReceiveV1) → MessageEvent → pipeline."""
        try:
            inner = event_data.event
            if inner is None:
                logger.debug("Feishu event dropped: inner is None, event_data=%s", type(event_data).__name__)
                return

            msg = inner.message
            if msg is None:
                logger.debug("Feishu event dropped: msg is None, inner=%s", type(inner).__name__)
                return

            content_raw = msg.content or "{}"
            try:
                content_obj = json.loads(content_raw)
                text = content_obj.get("text", "")
            except (json.JSONDecodeError, TypeError):
                text = str(content_raw)

            if not text:
                logger.debug("Feishu event dropped: empty text, msg_type=%s chat_id=%s",
                           getattr(msg, "message_type", "?"), getattr(msg, "chat_id", "?"))
                return

            # sender_id is a UserId object with open_id/union_id/user_id attrs
            sender = inner.sender
            user_id = ""
            if sender and sender.sender_id:
                uid = sender.sender_id
                user_id = uid.open_id or uid.union_id or uid.user_id or ""

            chat_type = msg.chat_type or "dm"
            logger.info("Feishu inbound: user=%s chat=%s type=%s text=%s",
                       user_id[:12] if user_id else "?",
                       (msg.chat_id or "")[:16], chat_type,
                       text[:60])

            source = SessionSource(
                platform="feishu",
                user_id=user_id,
                user_name="",
                chat_id=msg.chat_id or "",
                chat_type=chat_type,
            )

            event = MessageEvent(
                text=text,
                message_type="command" if text.startswith("/") else "text",
                source=source,
                raw_message=event_data,
                message_id=msg.message_id,
                timestamp=float(msg.create_time or time.time()),
            )
            self.handle_message(event)
        except Exception:
            logger.exception("_handle_feishu_event failed")

    # ── typing indicator ──────────────────────────────

    async def _send_typing(self, chat_id: str) -> None:
        """Feishu doesn't support typing indicators via bot API — no-op."""
        pass
