"""Builtin platform adapter behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import asyncio
import base64
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
        "qq_bot_ws_url": "ws://127.0.0.1:5701",
        "qq_bot_token": "",
        "qq_bot_webhook_secret": "",
        "platform_message_dedupe_max_size": 1024,
        "delivery_max_attempts": 3,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_feishu_disconnect_before_connect_is_idempotent(tmp_path: Path):
    from luna_agent.plugins.builtin.platforms.feishu.adapter import FeishuAdapter

    adapter = FeishuAdapter(_settings(tmp_path), db=None)

    await adapter.disconnect()

    health = adapter.health_snapshot()
    assert health["connected"] is False
    assert health["adapter"] == "FeishuAdapter"
    assert health["capabilities"]["text"] is True


@pytest.mark.asyncio
async def test_feishu_send_without_client_reports_not_connected(tmp_path: Path):
    from luna_agent.plugins.builtin.platforms.feishu.adapter import FeishuAdapter

    adapter = FeishuAdapter(_settings(tmp_path), db=None)

    result = await adapter.send("open-id", "hello")

    assert result.success is False
    assert "not connected" in (result.error or "").lower()


def test_feishu_receive_id_type_and_error_format():
    from luna_agent.plugins.builtin.platforms.feishu.adapter import (
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
    from luna_agent.models.messages import MessageEvent
    from luna_agent.plugins.builtin.platforms.feishu.adapter import FeishuAdapter

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
    assert captured[0].envelope is not None
    assert captured[0].envelope.text == "hello"


@pytest.mark.asyncio
async def test_feishu_image_message_preserves_attachment_reference(tmp_path: Path, monkeypatch):
    from luna_agent.plugins.builtin.platforms.feishu.adapter import FeishuAdapter

    adapter = FeishuAdapter(_settings(tmp_path), db=None)
    captured = []
    monkeypatch.setattr(adapter, "handle_message", lambda event: captured.append(event))
    event_data = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou-user", union_id="", user_id="")
            ),
            message=SimpleNamespace(
                content='{"image_key": "img-key", "file_size": 123}',
                message_type="image",
                chat_type="p2p",
                message_id="mid-1",
                chat_id="oc-chat",
                create_time="123456",
            ),
        )
    )

    await adapter._handle_feishu_event(event_data)

    assert len(captured) == 1
    event = captured[0]
    assert event.text == "[image: img-key]"
    assert [item.type for item in event.attachments] == ["image"]
    assert event.envelope.attachments[0].platform_file_id == "img-key"
    assert event.envelope.attachments[0].size == 123
    assert event.attachments[0].metadata["size"] == 123
    assert event.attachments[0].metadata["feishu_data"]["image_key"] == "img-key"


@pytest.mark.asyncio
async def test_feishu_file_message_preserves_name_and_mime(tmp_path: Path, monkeypatch):
    from luna_agent.plugins.builtin.platforms.feishu.adapter import FeishuAdapter

    adapter = FeishuAdapter(_settings(tmp_path), db=None)
    captured = []
    monkeypatch.setattr(adapter, "handle_message", lambda event: captured.append(event))
    event_data = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou-user", union_id="", user_id="")
            ),
            message=SimpleNamespace(
                content=json.dumps({
                    "file_key": "file-key",
                    "file_name": "report.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 456,
                }),
                message_type="file",
                chat_type="p2p",
                message_id="mid-2",
                chat_id="oc-chat",
                create_time="123457",
            ),
        )
    )

    await adapter._handle_feishu_event(event_data)

    attachment = captured[0].envelope.attachments[0]
    assert attachment.kind == "file"
    assert attachment.platform_file_id == "file-key"
    assert attachment.name == "report.pdf"
    assert attachment.mime_type == "application/pdf"


@pytest.mark.asyncio
async def test_telegram_after_parse_without_hooks_keeps_message_event(tmp_path: Path, monkeypatch):
    from luna_agent.models.messages import MessageEvent, SessionSource
    from luna_agent.plugins.builtin.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(_settings(tmp_path, telegram_bot_token="token"), db=None)
    captured = []
    monkeypatch.setattr(adapter, "handle_message", lambda event: captured.append(event))
    event = MessageEvent(
        text="hello",
        source=SessionSource(platform="telegram", user_id="u1", chat_id="c1"),
    )

    await adapter._handle_telegram_event(event, raw_update=object())

    assert captured == [event]
    assert event.envelope is not None
    assert event.envelope.text == "hello"


def test_telegram_message_parts_extracts_photo_document_and_voice():
    from luna_agent.plugins.builtin.platforms.telegram.adapter import _message_parts

    msg = SimpleNamespace(
        text="",
        caption="see",
        photo=[
            SimpleNamespace(file_id="small", file_unique_id="u-small", file_size=10, width=10, height=10),
            SimpleNamespace(file_id="big", file_unique_id="u-big", file_size=20, width=100, height=100),
        ],
        document=SimpleNamespace(
            file_id="doc-1",
            file_unique_id="doc-u",
            file_name="report.pdf",
            mime_type="application/pdf",
            file_size=300,
        ),
        voice=SimpleNamespace(
            file_id="voice-1",
            file_unique_id="voice-u",
            mime_type="audio/ogg",
            file_size=40,
            duration=2,
        ),
        audio=None,
        video=None,
    )

    text, parts, attachments = _message_parts(msg)

    assert text == "see"
    assert [item.type for item in attachments] == ["image", "file", "audio"]
    assert attachments[0].file_id == "big"
    assert attachments[1].name == "report.pdf"
    assert attachments[1].mime_type == "application/pdf"
    assert attachments[2].file_id == "voice-1"
    assert parts[0].text == "see"


def test_telegram_message_parts_allows_attachment_only_message():
    from luna_agent.plugins.builtin.platforms.telegram.adapter import _message_parts

    msg = SimpleNamespace(
        text="",
        caption="",
        photo=[SimpleNamespace(file_id="photo-1", file_unique_id="photo-u", file_size=10, width=1, height=1)],
        document=None,
        voice=None,
        audio=None,
        video=None,
    )

    text, parts, attachments = _message_parts(msg)

    assert text == "[image: photo-1]"
    assert [item.type for item in parts] == ["image"]
    assert attachments[0].file_id == "photo-1"


@pytest.mark.asyncio
async def test_wechat_connect_without_creds_reports_error(tmp_path: Path):
    from luna_agent.plugins.builtin.platforms.wechat.adapter import WeChatAdapter

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
    from luna_agent.plugins.builtin.platforms.wechat.adapter import WeChatAdapter

    adapter = WeChatAdapter(_settings(tmp_path), db=None)

    result = await adapter.send("wx-user", "hello")

    assert result.success is False
    assert "not connected" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_wechat_send_splits_long_text(tmp_path: Path, monkeypatch):
    from luna_agent.plugins.builtin.platforms.wechat.adapter import WeChatAdapter

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
async def test_wechat_send_splits_long_code_fence(tmp_path: Path, monkeypatch):
    from luna_agent.plugins.builtin.platforms.wechat.adapter import WeChatAdapter

    adapter = WeChatAdapter(_settings(tmp_path), db=None)
    adapter._send_session = object()
    calls = []

    async def fake_api(path, payload, session, timeout_ms):
        calls.append(payload["msg"]["item_list"][0]["text_item"]["text"])
        return {"ret": 0, "errcode": 0}

    monkeypatch.setattr(adapter, "_api", fake_api)
    content = "```\n" + ("a" * 4500) + "\n```"

    result = await adapter.send("wx-user", content)

    assert result.success is True
    assert len(calls) >= 3
    assert all(len(item) <= adapter.MAX_MESSAGE_LENGTH for item in calls)


@pytest.mark.asyncio
async def test_wechat_send_includes_cached_context_token(tmp_path: Path, monkeypatch):
    from luna_agent.plugins.builtin.platforms.wechat.adapter import WeChatAdapter

    adapter = WeChatAdapter(_settings(tmp_path), db=None)
    adapter._send_session = object()
    adapter._context_tokens["wx-user"] = "ctx-token"
    payloads = []

    async def fake_api(path, payload, session, timeout_ms):
        payloads.append(payload)
        return {"ret": 0, "errcode": 0}

    monkeypatch.setattr(adapter, "_api", fake_api)

    result = await adapter.send("wx-user", "hello")

    assert result.success is True
    assert payloads[0]["msg"]["context_token"] == "ctx-token"


@pytest.mark.asyncio
async def test_wechat_process_message_summarizes_media_and_caches_context(tmp_path: Path, monkeypatch):
    from luna_agent.plugins.builtin.platforms.wechat.adapter import WeChatAdapter

    adapter = WeChatAdapter(_settings(tmp_path), db=None)
    captured = []
    monkeypatch.setattr(adapter, "handle_message", lambda event: captured.append(event))

    await adapter._process_update({
        "from_user_id": "wx-user",
        "from_user_name": "User",
        "message_id": "m1",
        "create_time": 123456,
        "context_token": "ctx-token",
        "item_list": [
            {"type": 1, "text_item": {"text": "see"}},
            {"type": 2, "image_item": {"file_id": "img-1"}},
            {"type": 3, "voice_item": {"file_name": "voice.amr"}},
            {"type": 5, "video_item": {"media_id": "video-1"}},
            {"type": 4, "file_item": {"file_name": "report.pdf"}},
        ],
    })

    assert len(captured) == 1
    assert captured[0].text == (
        "see [image: img-1] [audio: voice.amr] [video: video-1] [file: report.pdf]"
    )
    assert [item.type for item in captured[0].attachments] == ["image", "audio", "video", "file"]
    assert captured[0].attachments[0].file_id == "img-1"
    assert captured[0].attachments[-1].name == "report.pdf"
    assert captured[0].envelope is not None
    assert [item.kind for item in captured[0].envelope.attachments] == ["image", "audio", "video", "file"]
    assert captured[0].envelope.attachments[0].platform_file_id == "img-1"
    assert captured[0].attachments[1].metadata["wechat_media"]["file_name"] == "voice.amr"
    assert adapter._context_tokens["wx-user"] == "ctx-token"


@pytest.mark.asyncio
async def test_wechat_update_enters_base_message_pipeline_once(tmp_path: Path, monkeypatch):
    from luna_agent.plugins.builtin.platforms.wechat.adapter import WeChatAdapter

    adapter = WeChatAdapter(_settings(tmp_path), db=None)
    handled = []
    finished = asyncio.Event()

    async def handler(event):
        handled.append(event)
        finished.set()
        return None

    async def no_typing(chat_id):
        return None

    adapter.set_message_handler(handler)
    monkeypatch.setattr(adapter, "_send_typing", no_typing)

    await adapter._process_update({
        "from_user_id": "wx-user",
        "message_id": "m1",
        "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
    })
    await asyncio.wait_for(finished.wait(), timeout=1)

    assert len(handled) == 1
    assert handled[0].text == "hello"
    assert handled[0].source.user_id == "wx-user"


@pytest.mark.asyncio
async def test_qq_connect_without_websocket_url_reports_error(tmp_path: Path):
    from luna_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

    adapter = QQAdapter(_settings(tmp_path, qq_bot_ws_url=""), db=None)

    with pytest.raises(RuntimeError, match="WebSocket URL"):
        await adapter.connect()

    health = adapter.health_snapshot()
    assert health["connected"] is False
    assert "WebSocket URL" in health["last_connect_error"]
    assert health["capabilities"]["image_send"] is True


@pytest.mark.asyncio
async def test_qq_websocket_connect_uses_bearer_token(tmp_path: Path):
    from luna_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

    adapter = QQAdapter(_settings(tmp_path, qq_bot_token="secret"), db=None)
    calls = []

    class FakeSession:
        async def ws_connect(self, url, **kwargs):
            calls.append((url, kwargs))
            return object()

    adapter._session = FakeSession()

    result = await adapter._open_websocket()

    assert result is not None
    assert calls[0][0] == "ws://127.0.0.1:5701"
    assert calls[0][1]["headers"] == {"Authorization": "Bearer secret"}
    assert calls[0][1]["heartbeat"] == 30


@pytest.mark.asyncio
async def test_qq_websocket_event_enters_message_pipeline(tmp_path: Path, monkeypatch):
    from luna_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

    adapter = QQAdapter(_settings(tmp_path, qq_bot_webhook_secret="legacy-secret"), db=None)
    captured = []
    monkeypatch.setattr(adapter, "handle_message", lambda event: captured.append(event))

    await adapter._handle_websocket_text(json.dumps({
        "time": 123456,
        "self_id": 90000,
        "post_type": "message",
        "message_type": "private",
        "user_id": 10001,
        "message_id": 99,
        "message": [{"type": "text", "data": {"text": "hello"}}],
    }))

    assert len(captured) == 1
    assert captured[0].text == "hello"
    assert captured[0].source.platform == "qq"
    assert adapter.health_snapshot()["self_id"] == "90000"
    assert adapter.health_snapshot()["last_ws_event_at"]


@pytest.mark.asyncio
async def test_qq_websocket_action_matches_echo_without_http(tmp_path: Path):
    from luna_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

    adapter = QQAdapter(_settings(tmp_path, qq_bot_base_url=""), db=None)
    sent = []

    class FakeWebSocket:
        closed = False

        async def send_json(self, payload):
            sent.append(payload)
            await adapter._handle_websocket_text(json.dumps({
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": 321},
                "echo": payload["echo"],
            }))

    adapter._session = object()
    adapter._websocket = FakeWebSocket()

    result = await adapter.send("private:10001", "hello")

    assert result.success is True
    assert result.message_id == "321"
    assert sent[0]["action"] == "send_private_msg"
    assert sent[0]["params"] == {"user_id": "10001", "message": "hello"}
    assert sent[0]["echo"]
    assert adapter._pending_actions == {}


@pytest.mark.asyncio
async def test_qq_websocket_reconnect_retries_until_available(tmp_path: Path, monkeypatch):
    from luna_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

    adapter = QQAdapter(_settings(tmp_path), db=None)
    adapter._reconnect_delays = (0,)
    websocket = SimpleNamespace(closed=False)
    attempts = 0

    async def fake_open():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("offline")
        return websocket

    monkeypatch.setattr(adapter, "_open_websocket", fake_open)

    result = await adapter._reconnect_websocket()

    assert result is websocket
    assert attempts == 2
    assert adapter.health_snapshot()["connected"] is True
    assert adapter.health_snapshot()["ws_reconnect_attempts"] == 2


@pytest.mark.asyncio
async def test_qq_send_builds_onebot_private_and_group_requests(tmp_path: Path, monkeypatch):
    from luna_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

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
    from luna_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

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
    from luna_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

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
    from luna_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

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
        "[audio: voice.amr]"
        "[video: movie.mp4]"
        "[file: report.pdf]"
        "[reply:42]"
    )
    assert [item.type for item in captured[0].attachments] == ["image", "audio", "video", "file"]
    assert captured[0].attachments[0].url == "https://example.test/a.png"
    assert captured[0].attachments[1].file_id == "voice.amr"
    assert captured[0].attachments[2].file_id == "movie.mp4"
    assert captured[0].attachments[-1].name == "report.pdf"
    assert captured[0].envelope is not None
    assert [item.kind for item in captured[0].envelope.attachments] == ["image", "audio", "video", "file"]
    assert captured[0].envelope.attachments[0].url == "https://example.test/a.png"


@pytest.mark.asyncio
async def test_qq_webhook_signature_is_checked(tmp_path: Path, monkeypatch):
    from luna_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

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


@pytest.mark.asyncio
async def test_qq_download_attachment_uses_onebot_media_endpoint(tmp_path: Path, monkeypatch):
    from luna_agent.models.messages import AttachmentRef
    from luna_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

    adapter = QQAdapter(_settings(tmp_path), db=None)
    adapter._session = object()
    calls = []

    async def fake_post_json(endpoint, payload):
        calls.append((endpoint, payload))
        return {"status": "ok", "data": {"file": "https://example.test/voice.amr"}}

    async def fake_read_download_target(target, **kwargs):
        return b"voice", target, "audio/amr"

    monkeypatch.setattr(adapter, "_post_json", fake_post_json)
    monkeypatch.setattr(adapter, "_read_download_target", fake_read_download_target)

    downloaded = await adapter.download_attachment(
        AttachmentRef(
            id="q1",
            kind="audio",
            name="voice.amr",
            platform_file_id="voice.amr",
            metadata={"onebot_data": {"file": "voice.amr"}},
        )
    )

    assert calls[0] == ("get_record", {"file": "voice.amr"})
    assert downloaded.data == b"voice"
    assert downloaded.kind == "audio"
    assert downloaded.mime_type == "audio/amr"
    assert downloaded.source_url == "https://example.test/voice.amr"


@pytest.mark.asyncio
async def test_wechat_download_attachment_decrypts_cdn_media(tmp_path: Path, monkeypatch):
    from Crypto.Cipher import AES

    from luna_agent.models.messages import AttachmentRef
    from luna_agent.plugins.builtin.platforms.wechat.adapter import WeChatAdapter

    key = b"0123456789abcdef"
    plaintext = b"image-payload"
    pad = 16 - (len(plaintext) % 16)
    encrypted = AES.new(key, AES.MODE_ECB).encrypt(plaintext + bytes([pad]) * pad)
    adapter = WeChatAdapter(_settings(tmp_path), db=None)

    async def fake_download_url_bytes(url, *, kind):
        return encrypted, "application/octet-stream"

    monkeypatch.setattr(adapter, "_download_url_bytes", fake_download_url_bytes)

    downloaded = await adapter.download_attachment(
        AttachmentRef(
            id="w1",
            kind="image",
            name="photo.png",
            metadata={
                "wechat_media": {
                    "file_name": "photo.png",
                    "media": {
                        "encrypt_query_param": "encrypted-param",
                        "aes_key": base64.b64encode(key).decode(),
                    },
                }
            },
        )
    )

    assert downloaded.data == plaintext
    assert downloaded.name == "photo.png"
    assert downloaded.source_url.startswith("https://novac2c.cdn.weixin.qq.com/c2c/download")
    assert downloaded.platform_file_id == "encrypted-param"
    assert downloaded.metadata["wechat_download"]["encrypted"] is True


@pytest.mark.asyncio
async def test_wechat_download_prefers_top_level_aeskey(tmp_path: Path, monkeypatch):
    from Crypto.Cipher import AES

    from luna_agent.models.messages import AttachmentRef
    from luna_agent.plugins.builtin.platforms.wechat.adapter import WeChatAdapter

    key = b"0123456789abcdef"
    wrong_key = b"abcdef0123456789"
    plaintext = b"image-payload"
    pad = 16 - (len(plaintext) % 16)
    encrypted = AES.new(key, AES.MODE_ECB).encrypt(plaintext + bytes([pad]) * pad)
    adapter = WeChatAdapter(_settings(tmp_path), db=None)

    async def fake_download_url_bytes(url, *, kind):
        return encrypted, "application/octet-stream"

    monkeypatch.setattr(adapter, "_download_url_bytes", fake_download_url_bytes)

    downloaded = await adapter.download_attachment(
        AttachmentRef(
            id="w-top-key",
            kind="image",
            name="photo.png",
            metadata={
                "wechat_media": {
                    "file_name": "photo.png",
                    "aeskey": base64.b64encode(key).decode(),
                    "media": {
                        "encrypt_query_param": "encrypted-param",
                        "aes_key": base64.b64encode(wrong_key).decode(),
                    },
                }
            },
        )
    )

    assert downloaded.data == plaintext


@pytest.mark.asyncio
async def test_wechat_download_accepts_unpadded_encrypted_media(tmp_path: Path, monkeypatch):
    from Crypto.Cipher import AES

    from luna_agent.models.messages import AttachmentRef
    from luna_agent.plugins.builtin.platforms.wechat.adapter import WeChatAdapter

    key = b"0123456789abcdef"
    plaintext = b"\x89PNG\r\n\x1a\npayload!"
    assert len(plaintext) % 16 == 0
    encrypted = AES.new(key, AES.MODE_ECB).encrypt(plaintext)
    adapter = WeChatAdapter(_settings(tmp_path), db=None)

    async def fake_download_url_bytes(url, *, kind):
        return encrypted, "application/octet-stream"

    monkeypatch.setattr(adapter, "_download_url_bytes", fake_download_url_bytes)

    downloaded = await adapter.download_attachment(
        AttachmentRef(
            id="w-unpadded",
            kind="image",
            metadata={
                "wechat_media": {
                    "encrypt_query_param": "encrypted-param",
                    "aeskey": base64.b64encode(key).decode(),
                }
            },
        )
    )

    assert downloaded.data == plaintext


@pytest.mark.asyncio
async def test_wechat_download_reads_all_response_chunks(tmp_path: Path, monkeypatch):
    from luna_agent.plugins.builtin.platforms.wechat.adapter import WeChatAdapter

    class Content:
        async def iter_chunked(self, size):
            yield b"a" * 3
            yield b"b" * 5
            yield b"c" * 7

    class Response:
        ok = True
        content = Content()
        headers = {"Content-Type": "image/jpeg"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Session:
        def get(self, url):
            return Response()

    adapter = WeChatAdapter(_settings(tmp_path), db=None)
    adapter._send_session = Session()

    data, mime_type = await adapter._download_url_bytes("https://example.com/image.jpg", kind="image")

    assert data == b"aaa" + b"bbbbb" + b"ccccccc"
    assert mime_type == "image/jpeg"


@pytest.mark.asyncio
async def test_wechat_download_attachment_requires_key_before_download(tmp_path: Path, monkeypatch):
    from luna_agent.models.messages import AttachmentRef
    from luna_agent.platforms import AttachmentDownloadError
    from luna_agent.plugins.builtin.platforms.wechat.adapter import WeChatAdapter

    adapter = WeChatAdapter(_settings(tmp_path), db=None)

    async def fake_download_url_bytes(url, *, kind):  # pragma: no cover - should not run
        raise AssertionError("download should not run without decrypt key")

    monkeypatch.setattr(adapter, "_download_url_bytes", fake_download_url_bytes)

    with pytest.raises(AttachmentDownloadError) as exc_info:
        await adapter.download_attachment(
            AttachmentRef(
                id="w-key",
                kind="image",
                metadata={
                    "wechat_media": {
                        "encrypt_query_param": "encrypted-param",
                    }
                },
            )
        )

    assert exc_info.value.reason == "decrypt_key_unavailable"


@pytest.mark.asyncio
async def test_wechat_prepare_encrypted_url_uses_platform_downloader(tmp_path: Path, monkeypatch):
    from luna_agent.attachments import AttachmentStore, DownloadedAttachment
    from luna_agent.models.messages import AttachmentRef
    from luna_agent.plugins.builtin.platforms.wechat.adapter import WeChatAdapter

    adapter = WeChatAdapter(_settings(tmp_path), db=None)
    adapter.set_attachment_store(AttachmentStore(tmp_path / "cache"))
    calls = []

    async def fake_download_attachment(ref, source=None):
        calls.append((ref.url, ref.platform_file_id))
        return DownloadedAttachment(
            data=b"\x89PNG\r\n\x1a\npayload",
            kind="image",
            name="photo.png",
            mime_type="image/png",
            platform_file_id=ref.platform_file_id,
        )

    monkeypatch.setattr(adapter, "download_attachment", fake_download_attachment)

    prepared = await adapter._prepare_attachment_ref(
        AttachmentRef(
            id="w2",
            kind="image",
            name="photo.png",
            url="https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param=encrypted-param",
            metadata={
                "wechat_media": {
                    "cdn_url": "https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param=encrypted-param",
                    "aes_key": base64.b64encode(b"0123456789abcdef").decode(),
                    "media_id": "encrypted-param",
                }
            },
        ),
        source=None,
    )

    assert calls == [("", "encrypted-param")]
    assert prepared.local_path
    assert prepared.metadata["attachment_resolve"]["status"] == "resolved"


@pytest.mark.asyncio
async def test_wechat_prepare_top_level_encrypted_param_uses_platform_downloader(tmp_path: Path, monkeypatch):
    from luna_agent.attachments import AttachmentStore, DownloadedAttachment
    from luna_agent.models.messages import AttachmentRef
    from luna_agent.plugins.builtin.platforms.wechat.adapter import WeChatAdapter

    adapter = WeChatAdapter(_settings(tmp_path), db=None)
    adapter.set_attachment_store(AttachmentStore(tmp_path / "cache"))
    calls = []

    async def fake_download_attachment(ref, source=None):
        calls.append((ref.url, ref.platform_file_id))
        return DownloadedAttachment(
            data=b"\x89PNG\r\n\x1a\npayload",
            kind="image",
            name="photo.png",
            mime_type="image/png",
            platform_file_id=ref.platform_file_id,
        )

    monkeypatch.setattr(adapter, "download_attachment", fake_download_attachment)

    prepared = await adapter._prepare_attachment_ref(
        AttachmentRef(
            id="w3",
            kind="image",
            name="photo.png",
            metadata={
                "wechat_media": {
                    "encrypt_query_param": "encrypted-param",
                    "aes_key": base64.b64encode(b"0123456789abcdef").decode(),
                }
            },
        ),
        source=None,
    )

    assert calls == [("", "encrypted-param")]
    assert prepared.local_path
    assert prepared.metadata["attachment_resolve"]["status"] == "resolved"
