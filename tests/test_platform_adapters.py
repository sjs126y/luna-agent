"""Builtin platform adapter behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import hashlib
import hmac
import json

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
async def test_feishu_after_parse_without_hooks_keeps_message_event(tmp_path: Path, monkeypatch):
    from personal_agent.models.messages import MessageEvent
    from personal_agent.plugins.builtin.platforms.feishu.adapter import FeishuAdapter

    adapter = FeishuAdapter(_settings(tmp_path), db=None)
    captured = []
    monkeypatch.setattr(adapter, "handle_message", lambda event: captured.append(event))
    event_data = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou-user", union_id="", user_id="")
            ),
            message=SimpleNamespace(
                content='{"text": "hello"}',
                chat_type="p2p",
                message_id="mid-1",
                chat_id="oc-chat",
                create_time="123456",
            ),
        )
    )

    await adapter._handle_feishu_event(event_data)

    assert len(captured) == 1
    assert isinstance(captured[0], MessageEvent)
    assert captured[0].text == "hello"
    assert captured[0].source.platform == "feishu"


@pytest.mark.asyncio
async def test_telegram_after_parse_without_hooks_keeps_message_event(tmp_path: Path, monkeypatch):
    from personal_agent.models.messages import MessageEvent, SessionSource
    from personal_agent.plugins.builtin.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(_settings(tmp_path, telegram_bot_token="token"), db=None)
    captured = []
    monkeypatch.setattr(adapter, "handle_message", lambda event: captured.append(event))
    event = MessageEvent(
        text="hello",
        source=SessionSource(platform="telegram", user_id="u1", chat_id="c1"),
    )

    await adapter._handle_telegram_event(event, raw_update=object())

    assert captured == [event]


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


@pytest.mark.asyncio
async def test_qq_connect_without_base_url_reports_error(tmp_path: Path):
    from personal_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

    adapter = QQAdapter(_settings(tmp_path, qq_bot_base_url=""), db=None)

    with pytest.raises(RuntimeError, match="base URL"):
        await adapter.connect()

    health = adapter.health_snapshot()
    assert health["connected"] is False
    assert "base URL" in health["last_connect_error"]


@pytest.mark.asyncio
async def test_qq_send_builds_onebot_private_and_group_requests(tmp_path: Path, monkeypatch):
    from personal_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

    adapter = QQAdapter(_settings(tmp_path), db=None)
    adapter._session = object()
    calls = []

    async def fake_post_json(endpoint, payload):
        calls.append((endpoint, payload))
        return {"status": "ok", "message_id": 123}

    monkeypatch.setattr(adapter, "_post_json", fake_post_json)

    private = await adapter.send("private:10001", "hello")
    group = await adapter.send("group:20002", "hi")

    assert private.success is True
    assert private.message_id == "123"
    assert group.success is True
    assert calls == [
        ("send_private_msg", {"user_id": "10001", "message": "hello"}),
        ("send_group_msg", {"group_id": "20002", "message": "hi"}),
    ]


@pytest.mark.asyncio
async def test_qq_send_builds_onebot_rich_segments(tmp_path: Path, monkeypatch):
    from personal_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

    adapter = QQAdapter(_settings(tmp_path), db=None)
    adapter._session = object()
    calls = []

    async def fake_post_json(endpoint, payload):
        calls.append((endpoint, payload))
        return {"status": "ok"}

    monkeypatch.setattr(adapter, "_post_json", fake_post_json)

    result = await adapter.send(
        "group:20002",
        "hello [at:10001] ![chart](https://example.test/a.png) [reply:42] [voice:file:///tmp/a.amr]",
    )

    assert result.success is True
    assert calls[0][0] == "send_group_msg"
    assert calls[0][1]["message"] == [
        {"type": "text", "data": {"text": "hello "}},
        {"type": "at", "data": {"qq": "10001"}},
        {"type": "text", "data": {"text": " "}},
        {"type": "image", "data": {"file": "https://example.test/a.png"}},
        {"type": "text", "data": {"text": " "}},
        {"type": "reply", "data": {"id": "42"}},
        {"type": "text", "data": {"text": " "}},
        {"type": "record", "data": {"file": "file:///tmp/a.amr"}},
    ]


@pytest.mark.asyncio
async def test_qq_webhook_payload_parses_onebot_message(tmp_path: Path, monkeypatch):
    from personal_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

    adapter = QQAdapter(_settings(tmp_path), db=None)
    captured = []
    monkeypatch.setattr(adapter, "handle_message", lambda event: captured.append(event))

    handled = await adapter.handle_webhook_payload({
        "post_type": "message",
        "message_type": "group",
        "group_id": 20002,
        "user_id": 10001,
        "message_id": 99,
        "time": 123456,
        "sender": {"nickname": "Neo"},
        "message": [
            {"type": "text", "data": {"text": "/ping"}},
        ],
    })

    assert handled is True
    assert len(captured) == 1
    event = captured[0]
    assert event.text == "/ping"
    assert event.message_type == "command"
    assert event.source.platform == "qq"
    assert event.source.chat_id == "group:20002"
    assert event.source.user_id == "10001"
    assert event.source.user_name == "Neo"


@pytest.mark.asyncio
async def test_qq_webhook_payload_summarizes_media_segments(tmp_path: Path, monkeypatch):
    from personal_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

    adapter = QQAdapter(_settings(tmp_path), db=None)
    captured = []
    monkeypatch.setattr(adapter, "handle_message", lambda event: captured.append(event))

    handled = await adapter.handle_webhook_payload({
        "post_type": "message",
        "message_type": "private",
        "user_id": 10001,
        "message": [
            {"type": "text", "data": {"text": "see "}},
            {"type": "image", "data": {"url": "https://example.test/a.png"}},
            {"type": "record", "data": {"file": "voice.amr"}},
            {"type": "video", "data": {"file": "movie.mp4"}},
            {"type": "file", "data": {"name": "report.pdf"}},
            {"type": "reply", "data": {"id": "42"}},
        ],
    })

    assert handled is True
    assert captured[0].text == (
        "see [image: https://example.test/a.png]"
        "[voice: voice.amr]"
        "[video: movie.mp4]"
        "[file: report.pdf]"
        "[reply:42]"
    )


@pytest.mark.asyncio
async def test_qq_webhook_signature_is_checked(tmp_path: Path, monkeypatch):
    from personal_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

    secret = "webhook-secret"
    adapter = QQAdapter(_settings(tmp_path, qq_bot_webhook_secret=secret), db=None)
    captured = []
    monkeypatch.setattr(adapter, "handle_message", lambda event: captured.append(event))
    payload = {
        "post_type": "message",
        "message_type": "private",
        "user_id": 10001,
        "raw_message": "hello",
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    signature = "sha1=" + hmac.new(secret.encode(), body.encode(), hashlib.sha1).hexdigest()

    assert await adapter.handle_webhook_payload(payload, signature="bad") is False
    assert await adapter.handle_webhook_payload(payload, signature=signature) is True
    assert len(captured) == 1
