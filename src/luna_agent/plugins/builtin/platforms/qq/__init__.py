"""QQ platform plugin entrypoint."""

from urllib.parse import urlparse

from luna_agent.platforms.setup import PlatformSetupContext, PlatformSetupResult


def _setup(context: PlatformSetupContext) -> PlatformSetupResult:
    ws_url = context.prompt("QQ Bot WebSocket URL", default=context.env_value("QQ_BOT_WS_URL"))
    token = context.prompt_secret("QQ Bot Token", default=context.env_value("QQ_BOT_TOKEN"))
    parsed = urlparse(ws_url)
    if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
        raise ValueError("QQ_BOT_WS_URL must be a ws:// or wss:// URL")
    if not token:
        raise ValueError("QQ_BOT_TOKEN is required")
    if not context.env_value("QQ_BOT_WS_URL"):
        context.set_env("QQ_BOT_WS_URL", ws_url)
    if not context.env_value("QQ_BOT_TOKEN"):
        context.set_env("QQ_BOT_TOKEN", token)
    return PlatformSetupResult(
        platform="qq",
        status="configured",
        configured=["QQ_BOT_WS_URL", "QQ_BOT_TOKEN"],
        credential_source=str(context.env_path),
        message="QQ/NapCat WebSocket 凭据已配置。",
    )


def register(ctx) -> None:
    from luna_agent.platforms.core import PlatformEntry
    from .adapter import QQAdapter
    from .companion import NapCatCompanion
    from .config import QQPluginConfig

    plugin_config = ctx.parse_config(QQPluginConfig)
    companion = NapCatCompanion(
        plugin_config.runtime,
        data_dir=ctx.settings.agent_data_dir,
    )

    def _factory(config, db):
        return QQAdapter(config, db, companion=companion)

    def _check(config):
        return bool(getattr(config, "qq_bot_ws_url", ""))

    ctx.register.platform(PlatformEntry(
        name="qq",
        factory=_factory,
        check_fn=_check,
        capabilities=QQAdapter.capabilities,
        setup_fn=_setup,
    ))
