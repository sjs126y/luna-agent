"""Feishu platform plugin entrypoint."""

from luna_agent.platforms.setup import PlatformSetupContext, PlatformSetupResult


def _setup(context: PlatformSetupContext) -> PlatformSetupResult:
    app_id = context.prompt("Feishu App ID", default=context.env_value("FEISHU_APP_ID"))
    app_secret = context.prompt_secret(
        "Feishu App Secret",
        default=context.env_value("FEISHU_APP_SECRET"),
    )
    if not app_id or not app_secret:
        raise ValueError("FEISHU_APP_ID and FEISHU_APP_SECRET are required")
    if not context.env_value("FEISHU_APP_ID"):
        context.set_env("FEISHU_APP_ID", app_id)
    if not context.env_value("FEISHU_APP_SECRET"):
        context.set_env("FEISHU_APP_SECRET", app_secret)
    return PlatformSetupResult(
        platform="feishu",
        status="configured",
        configured=["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
        credential_source=str(context.env_path),
        message="飞书应用凭据已配置。",
    )


def register(ctx) -> None:
    from luna_agent.platforms.core import PlatformEntry
    from .adapter import FeishuAdapter

    def _factory(config, db):
        return FeishuAdapter(config, db)

    def _check(config):
        return bool(getattr(config, "feishu_app_id", "") and getattr(config, "feishu_app_secret", ""))

    ctx.register.platform(PlatformEntry(
        name="feishu",
        factory=_factory,
        check_fn=_check,
        capabilities=FeishuAdapter.capabilities,
        setup_fn=_setup,
    ))
