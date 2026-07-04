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


@pytest.mark.asyncio
async def test_wechat_connect_without_creds_reports_error(tmp_path: Path):
    from personal_agent.plugins.builtin.platforms.wechat.adapter import WeChatAdapter

    adapter = WeChatAdapter(
        _settings(tmp_path, weixin_token="", weixin_account_id="", weixin_user_id=""),
        db=None,
    )

    with pytest.raises(RuntimeError, match="not logged in"):
        await adapter.connect()

    health = adapter.health_snapshot()
    assert health["connected"] is False
    assert "not logged in" in health["last_connect_error"]


@pytest.mark.asyncio
async def test_wechat_send_without_session_reports_not_connected(tmp_path: Path):
    from personal_agent.plugins.builtin.platforms.wechat.adapter import WeChatAdapter

    adapter = WeChatAdapter(_settings(tmp_path), db=None)

    result = await adapter.send("wx-user", "hello")

    assert result.success is False
    assert "not connected" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_wechat_send_splits_long_text(tmp_path: Path, monkeypatch):
    from personal_agent.plugins.builtin.platforms.wechat.adapter import WeChatAdapter

    adapter = WeChatAdapter(_settings(tmp_path), db=None)
    adapter._send_session = object()
    calls = []

    async def fake_api(path, payload, session, timeout_ms):
        calls.append(payload["msg"]["item_list"][0]["text_item"]["text"])
        return {"ret": 0, "errcode": 0}

    monkeypatch.setattr(adapter, "_api", fake_api)
    content = ("a" * 1900) + "\n" + ("b" * 1900)

    result = await adapter.send("wx-user", content)

    assert result.success is True
    assert len(calls) == 2
    assert all(len(item) <= adapter.MAX_MESSAGE_LENGTH for item in calls)
