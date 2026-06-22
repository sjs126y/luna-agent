"""Feishu adapter — auto-registers on import."""

from personal_agent.adapters.base import PlatformEntry, platform_registry
from personal_agent.adapters.feishu.adapter import FeishuAdapter


def _factory(config, db):
    return FeishuAdapter(config, db)


def _check(config):
    return bool(getattr(config, "feishu_app_id", "") and getattr(config, "feishu_app_secret", ""))


platform_registry.register(PlatformEntry(
    name="feishu",
    factory=_factory,
    check_fn=_check,
))
