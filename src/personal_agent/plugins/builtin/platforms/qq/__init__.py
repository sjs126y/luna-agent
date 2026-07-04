"""QQ platform plugin entrypoint."""


def register(ctx) -> None:
    from personal_agent.platforms.core import PlatformEntry
    from .adapter import QQAdapter

    def _factory(config, db):
        return QQAdapter(config, db)

    def _check(config):
        return bool(getattr(config, "qq_bot_base_url", ""))

    ctx.register_platform(PlatformEntry(
        name="qq",
        factory=_factory,
        check_fn=_check,
    ))
