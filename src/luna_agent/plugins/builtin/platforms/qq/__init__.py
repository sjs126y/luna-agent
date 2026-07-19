"""QQ platform plugin entrypoint."""


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
    ))
