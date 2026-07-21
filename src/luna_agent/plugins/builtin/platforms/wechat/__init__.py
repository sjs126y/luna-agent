"""WeChat platform plugin entrypoint."""

from luna_agent.platforms.setup import PlatformSetupContext, PlatformSetupResult


async def _setup(context: PlatformSetupContext) -> PlatformSetupResult:
    import json
    from .adapter import wechat_qr_login

    creds = await wechat_qr_login(
        context.data_dir,
        context.env_value("WEIXIN_BASE_URL", "https://ilinkai.weixin.qq.com"),
    )
    if not creds:
        raise RuntimeError("微信二维码登录未完成")
    path = context.write_credentials("creds.json", json.dumps(creds, indent=2))
    return PlatformSetupResult(
        platform="wechat",
        status="configured",
        configured=["WEIXIN_TOKEN", "WEIXIN_ACCOUNT_ID", "WEIXIN_USER_ID"],
        credential_source=str(path),
        message="微信已完成扫码配对。",
    )

def register(ctx) -> None:
    from luna_agent.platforms.core import PlatformEntry
    from .adapter import WeChatAdapter

    def _factory(config, db):
        return WeChatAdapter(config, db)

    def _check(config):
        if config.weixin_token and config.weixin_account_id:
            return True
        creds_path = config.agent_data_dir / "wechat" / "creds.json"
        return creds_path.exists()

    ctx.register.platform(PlatformEntry(
        name="wechat",
        factory=_factory,
        check_fn=_check,
        capabilities=WeChatAdapter.capabilities,
        setup_fn=_setup,
    ))
