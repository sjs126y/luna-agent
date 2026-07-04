"""Builtin platform adapter behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def _settings(tmp_path: Path, **overrides):
    values = {
        "agent_data_dir": tmp_path / "data",
        "feishu_app_id": "app-id",
        "feishu_app_secret": "app-secret",
        "weixin_token": "wx-token",
        "weixin_account_id": "wx-account",
        "weixin_user_id": "wx-user",
        "weixin_base_url": "https://ilinkai.weixin.qq.com",
        "qq_bot_base_url": "http://127.0.0.1:5700",
        "qq_bot_token": "",
        "qq_bot_webhook_secret": "",
        "platform_chat_locks_maxsize": 64,
        "platform_pending_warning_threshold": 10,
        "platform_message_dedupe_max_size": 1024,
        "platform_send_max_retries": 2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_feishu_disconnect_before_connect_is_idempotent(tmp_path: Path):
    from personal_agent.plugins.builtin.platforms.feishu.adapter import FeishuAdapter

    adapter = FeishuAdapter(_settings(tmp_path), db=None)

    await adapter.disconnect()

    health = adapter.health_snapshot()
    assert health["connected"] is False
    assert health["adapter"] == "FeishuAdapter"


@pytest.mark.asyncio
async def test_feishu_send_without_client_reports_not_connected(tmp_path: Path):
    from personal_agent.plugins.builtin.platforms.feishu.adapter import FeishuAdapter

    adapter = FeishuAdapter(_settings(tmp_path), db=None)

    result = await adapter.send("open-id", "hello")

    assert result.success is False
    assert "not connected" in (result.error or "").lower()


def test_feishu_receive_id_type_and_error_format():
    from personal_agent.plugins.builtin.platforms.feishu.adapter import (
        _receive_id_type,
        _response_error,
    )

    assert _receive_id_type("oc_123") == "chat_id"
    assert _receive_id_type("ou_123") == "open_id"
    assert _response_error(SimpleNamespace(code=999, msg="bad request")) == (
        "Feishu API error: 999 bad request"
    )
