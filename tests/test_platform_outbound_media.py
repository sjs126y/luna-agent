from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


class _Result:
    def __init__(self, *, data=None, ok=True):
        self.data = data or SimpleNamespace()
        self._ok = ok
        self.code = 0 if ok else 1
        self.msg = "" if ok else "failed"

    def success(self):
        return self._ok


@pytest.mark.asyncio
async def test_telegram_sends_native_image(tmp_path):
    from personal_agent.config import Settings
    from personal_agent.plugins.builtin.platforms.telegram.adapter import TelegramAdapter

    path = tmp_path / "image.png"
    path.write_bytes(b"png")

    class Bot:
        async def send_photo(self, **kwargs):
            assert kwargs["photo"].read() == b"png"
            return SimpleNamespace(message_id=42)

    adapter = TelegramAdapter(Settings(telegram_bot_token="token"), db=None)
    adapter._bot = Bot()
    result = await adapter.send_artifact(
        "1", kind="image", path=path, filename="image.png", mime_type="image/png"
    )

    assert result.success
    assert result.message_id == "42"


@pytest.mark.asyncio
async def test_feishu_uploads_then_sends_native_image(tmp_path):
    from personal_agent.config import Settings
    from personal_agent.plugins.builtin.platforms.feishu.adapter import FeishuAdapter

    path = tmp_path / "image.png"
    path.write_bytes(b"png")
    calls = []

    class Resource:
        def __init__(self, kind):
            self.kind = kind

        def create(self, request):
            calls.append(self.kind)
            if self.kind == "image":
                return _Result(data=SimpleNamespace(image_key="img-key"))
            return _Result(data=SimpleNamespace(message_id="msg-1"))

    client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(
        image=Resource("image"),
        file=Resource("file"),
        message=Resource("message"),
    )))
    adapter = FeishuAdapter(Settings(), db=None)
    adapter._lark_client = client
    result = await adapter.send_artifact(
        "ou_user", kind="image", path=path, filename="image.png", mime_type="image/png"
    )

    assert result.success
    assert result.message_id == "msg-1"
    assert calls == ["image", "message"]


@pytest.mark.asyncio
async def test_qq_sends_onebot_media_segment(tmp_path, monkeypatch):
    from personal_agent.config import Settings
    from personal_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

    path = tmp_path / "image.png"
    path.write_bytes(b"png")
    adapter = QQAdapter(Settings(qq_bot_base_url="http://localhost"), db=None)
    adapter._session = object()
    captured = {}

    async def post(endpoint, payload):
        captured.update({"endpoint": endpoint, "payload": payload})
        return {"status": "ok", "retcode": 0, "data": {"message_id": 7}}

    monkeypatch.setattr(adapter, "_post_json", post)
    result = await adapter.send_artifact(
        "group:2", kind="image", path=path, filename="image.png", mime_type="image/png"
    )

    assert result.success
    assert captured["endpoint"] == "send_group_msg"
    segment = captured["payload"]["message"][0]
    assert segment["type"] == "image"
    assert segment["data"]["file"].startswith("file://")


@pytest.mark.asyncio
async def test_wechat_encrypts_upload_and_sends_image_item(tmp_path, monkeypatch):
    from personal_agent.config import Settings
    from personal_agent.plugins.builtin.platforms.wechat.adapter import WeChatAdapter

    path = tmp_path / "image.png"
    path.write_bytes(b"plain-image")
    adapter = WeChatAdapter(Settings(), db=None)
    api_calls = []

    async def api(endpoint, payload, session, timeout):
        api_calls.append((endpoint, payload))
        if endpoint.endswith("getuploadurl"):
            return {"upload_param": "upload-token"}
        return {}

    class Response:
        status = 200
        headers = {"x-encrypted-param": "download-token"}

        async def text(self):
            return ""

    class Context:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Session:
        def __init__(self):
            self.upload = None

        def post(self, url, *, data, headers, timeout):
            self.upload = (url, bytes(data), headers)
            return Context()

    session = Session()
    adapter._send_session = session
    monkeypatch.setattr(adapter, "_api", api)
    result = await adapter.send_artifact(
        "wx-user", kind="image", path=path, filename="image.png", mime_type="image/png"
    )

    assert result.success
    assert [call[0] for call in api_calls] == [
        "ilink/bot/getuploadurl",
        "ilink/bot/sendmessage",
    ]
    upload_request = api_calls[0][1]
    assert upload_request["rawsize"] == len(b"plain-image")
    assert upload_request["filesize"] == 16
    assert len(session.upload[1]) == 16
    image_item = api_calls[1][1]["msg"]["item_list"][0]
    assert image_item["type"] == 2
    assert image_item["image_item"]["media"]["encrypt_query_param"] == "download-token"
    assert "aes_key" in image_item["image_item"]["media"]
