"""WeChat platform plugin entrypoint."""


def register(ctx) -> None:
    from personal_agent.adapters.base import PlatformEntry
    from personal_agent.adapters.wechat.adapter import WeChatAdapter

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
    ))
