"""Telegram platform plugin entrypoint."""


def register(ctx) -> None:
    from personal_agent.adapters.base import PlatformEntry
    from .adapter import TelegramAdapter

    def _factory(config, db):
        return TelegramAdapter(config, db)

    def _check(config):
        return bool(getattr(config, "telegram_bot_token", ""))

    ctx.register_platform(PlatformEntry(
        name="telegram",
        factory=_factory,
        check_fn=_check,
    ))
