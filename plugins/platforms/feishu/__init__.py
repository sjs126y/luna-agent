"""Feishu platform plugin entrypoint."""


def register(ctx) -> None:
    from personal_agent.adapters.base import PlatformEntry
    from personal_agent.adapters.feishu.adapter import FeishuAdapter

    def _factory(config, db):
        return FeishuAdapter(config, db)

    def _check(config):
        return bool(getattr(config, "feishu_app_id", "") and getattr(config, "feishu_app_secret", ""))

    ctx.register_platform(PlatformEntry(
        name="feishu",
        factory=_factory,
        check_fn=_check,
    ))
