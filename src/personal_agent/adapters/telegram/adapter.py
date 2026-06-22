"""Telegram adapter — python-telegram-bot integration. Stub for now."""

from __future__ import annotations

import logging

from personal_agent.adapters.base import BasePlatformAdapter, ChatInfo, SendResult

logger = logging.getLogger(__name__)


class TelegramAdapter(BasePlatformAdapter):
    """PTB-based adapter. bridge pattern same as Feishu: PTB callback thread
    → run_coroutine_threadsafe → main loop.
    """

    async def connect(self) -> None:
        self._loop = __import__("asyncio").get_running_loop()
        token = getattr(self.config, "telegram_bot_token", "")
        if not token:
            logger.warning("Telegram bot token not configured")
            return
        logger.info("Telegram adapter connecting...")
        # TODO: PTB Application setup
        # app = Application.builder().token(token).build()
        # app.add_handler(MessageHandler(filters.TEXT, self._on_update))
        # await app.initialize()
        # await app.start()

    async def disconnect(self) -> None:
        logger.info("Telegram adapter disconnected")

    async def send(self, chat_id: str, content: str) -> SendResult:
        # TODO: bot.send_message
        return SendResult(success=False, error="Telegram adapter not yet implemented")

    async def get_chat_info(self, chat_id: str) -> ChatInfo:
        return ChatInfo(chat_id=chat_id, chat_type="dm")

    def _on_update(self, update, context):
        """PTB callback → bridge to main loop."""
        # parse update → MessageEvent → self.handle_message(event)
        pass
