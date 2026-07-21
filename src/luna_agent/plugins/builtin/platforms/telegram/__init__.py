"""Telegram platform plugin entrypoint."""

from luna_agent.platforms.setup import PlatformSetupContext, PlatformSetupResult


def _setup(context: PlatformSetupContext) -> PlatformSetupResult:
    token = context.prompt_secret(
        "Telegram Bot Token",
        default=context.env_value("TELEGRAM_BOT_TOKEN"),
    )
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")
    if not context.env_value("TELEGRAM_BOT_TOKEN"):
        context.set_env("TELEGRAM_BOT_TOKEN", token)
    return PlatformSetupResult(
        platform="telegram",
        status="configured",
        configured=["TELEGRAM_BOT_TOKEN"],
        credential_source=str(context.env_path),
        message="Telegram Bot Token 已配置。",
    )


def register(ctx) -> None:
    from luna_agent.platforms.core import PlatformEntry
    from .adapter import TelegramAdapter

    def _factory(config, db):
        return TelegramAdapter(config, db)

    def _check(config):
        return bool(getattr(config, "telegram_bot_token", ""))

    ctx.register.platform(PlatformEntry(
        name="telegram",
        factory=_factory,
        check_fn=_check,
        capabilities=TelegramAdapter.capabilities,
        setup_fn=_setup,
    ))
