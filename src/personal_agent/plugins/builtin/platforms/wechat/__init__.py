"""WeChat platform plugin entrypoint."""


async def _wechat_qr_login(settings=None, **kwargs):
    if settings is None:
        return None

    from .adapter import wechat_qr_login

    return await wechat_qr_login(
        settings.agent_data_dir / "wechat",
        settings.weixin_base_url,
    )


def register(ctx) -> None:
    from personal_agent.platforms.core import PlatformEntry
    from .adapter import WeChatAdapter

    def _factory(config, db):
        return WeChatAdapter(config, db)

    def _check(config):
        if config.weixin_token and config.weixin_account_id:
            return True
        creds_path = config.agent_data_dir / "wechat" / "creds.json"
        return creds_path.exists()

    ctx.register_platform(PlatformEntry(
        name="wechat",
        factory=_factory,
        check_fn=_check,
        capabilities=WeChatAdapter.capabilities,
    ))
    ctx.register_hook("wechat_qr_login", _wechat_qr_login, priority=10)
