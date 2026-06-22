"""Telegram adapter — auto-registers on import."""

from personal_agent.adapters.base import PlatformEntry, platform_registry
from personal_agent.adapters.telegram.adapter import TelegramAdapter


def _factory(config, db):
    return TelegramAdapter(config, db)


def _check(config):
    return bool(getattr(config, "telegram_bot_token", ""))


platform_registry.register(PlatformEntry(
    name="telegram",
    factory=_factory,
    check_fn=_check,
))
