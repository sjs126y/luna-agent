"""Platform runtime import boundaries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


def test_platform_core_is_public_import_path():
    from luna_agent.platforms.core import (
        BasePlatformAdapter,
        PlatformCapabilities,
        PlatformEntry,
        platform_registry,
    )

    assert BasePlatformAdapter.__name__ == "BasePlatformAdapter"
    assert PlatformEntry.__name__ == "PlatformEntry"
    assert PlatformCapabilities.__name__ == "PlatformCapabilities"
    assert platform_registry is not None


def test_message_event_to_envelope_preserves_text_and_attachments():
    from luna_agent.models.messages import MessageEvent, MessagePart, SessionSource

    event = MessageEvent(
        text="see [image: https://example.test/a.png]",
        source=SessionSource(platform="qq", user_id="u1", chat_id="group:1", thread_id="t1"),
        parts=[
            MessagePart(type="text", text="see "),
            MessagePart(type="image", url="https://example.test/a.png"),
        ],
        attachments=[MessagePart(type="image", url="https://example.test/a.png", name="a.png")],
        message_id="m1",
        reply_to_text="previous",
    )

    envelope = event.to_envelope()

    assert event.envelope is envelope
    assert envelope.id == "m1"
    assert envelope.source.platform == "qq"
    assert envelope.thread_id == "t1"
    assert envelope.reply_to == "previous"
    assert envelope.render_text() == event.text
    assert envelope.attachments[0].id == "m1:1"
    assert envelope.attachments[0].kind == "image"
    assert envelope.attachments[0].url == "https://example.test/a.png"
    assert envelope.as_dict()["attachments"][0]["name"] == "a.png"


def test_response_envelope_renders_text_fallback():
    from luna_agent.models.messages import MessagePart, ResponseEnvelope

    response = ResponseEnvelope(parts=[
        MessagePart(type="text", text="file "),
        MessagePart(type="file", name="report.pdf"),
    ])

    assert response.render_text() == "file [file: report.pdf]"
    assert response.as_dict()["parts"][1]["type"] == "file"


@pytest.mark.asyncio
async def test_base_adapter_send_message_falls_back_to_text():
    from luna_agent.models.messages import MessagePart, OutboundMessage
    from luna_agent.platforms.core import BasePlatformAdapter, ChatInfo, SendResult

    class Adapter(BasePlatformAdapter):
        def __init__(self):
            super().__init__(SimpleNamespace(), db=None)
            self.sent = []

        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def send(self, chat_id: str, content: str) -> SendResult:
            self.sent.append((chat_id, content))
            return SendResult(success=True, message_id="m1")

        async def get_chat_info(self, chat_id: str) -> ChatInfo:
            return ChatInfo(chat_id=chat_id)

    adapter = Adapter()
    result = await adapter.send_message(
        "chat",
        OutboundMessage(parts=[
            MessagePart(type="text", text="see "),
            MessagePart(type="image", url="https://example.test/a.png"),
        ]),
    )

    assert result.success is True
    assert adapter.sent == [("chat", "see [image: https://example.test/a.png]")]
    assert adapter.health_snapshot()["capabilities"]["text"] is True


@pytest.mark.asyncio
async def test_adapter_forwards_messages_without_owning_session_order():
    from luna_agent.models.messages import MessageEvent, SessionSource
    from luna_agent.platforms.core import BasePlatformAdapter, ChatInfo, SendResult

    class Adapter(BasePlatformAdapter):
        async def connect(self): pass
        async def disconnect(self): pass
        async def send(self, chat_id, content): return SendResult(success=True)
        async def get_chat_info(self, chat_id): return ChatInfo(chat_id=chat_id)

    adapter = Adapter(SimpleNamespace(), db=None)
    started = []
    release = asyncio.Event()

    async def handler(event):
        started.append(event.text)
        if event.text == "work":
            await release.wait()

    adapter.set_message_handler(handler)
    source = SessionSource(platform="test", user_id="u1", chat_id="c1")
    adapter.handle_message(MessageEvent(text="work", source=source))
    adapter.handle_message(MessageEvent(text="/stop", source=source))
    await asyncio.sleep(0)

    assert started == ["work", "/stop"]
    release.set()


@pytest.mark.asyncio
async def test_base_adapter_prepare_inbound_attachment_downloads_to_store(tmp_path):
    from luna_agent.attachments import AttachmentStore, DownloadedAttachment
    from luna_agent.models.messages import MessageEvent, MessagePart, SessionSource
    from luna_agent.platforms.core import BasePlatformAdapter, ChatInfo, SendResult

    class Adapter(BasePlatformAdapter):
        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def send(self, chat_id: str, content: str) -> SendResult:
            return SendResult(success=True)

        async def get_chat_info(self, chat_id: str) -> ChatInfo:
            return ChatInfo(chat_id=chat_id)

        async def download_attachment(self, ref, source=None) -> DownloadedAttachment:
            return DownloadedAttachment(
                data=b"\x89PNG\r\n\x1a\npayload",
                kind="image",
                name="photo.png",
                mime_type="image/png",
                platform_file_id=ref.platform_file_id,
                metadata={"downloaded_by": "test"},
            )

    adapter = Adapter(SimpleNamespace(agent_data_dir=tmp_path / "data"), db=None)
    adapter.set_attachment_store(AttachmentStore(tmp_path / "cache"))
    event = MessageEvent(
        text="[image: file-1]",
        source=SessionSource(platform="test", user_id="u1", chat_id="c1"),
        attachments=[MessagePart(type="image", file_id="file-1", name="photo.png")],
        message_id="m1",
    )

    prepared = await adapter.prepare_inbound_attachments(event)
    attachment = prepared.envelope.attachments[0]

    assert attachment.local_path
    assert attachment.mime_type == "image/png"
    assert attachment.metadata["downloaded_by"] == "test"
    assert attachment.metadata["attachment_resolve"]["status"] == "resolved"
    assert attachment.metadata["attachment_resolve"]["sha256"]


@pytest.mark.asyncio
async def test_base_adapter_prepare_inbound_attachment_respects_off_mode(tmp_path):
    from luna_agent.attachments import AttachmentStore, DownloadedAttachment
    from luna_agent.models.messages import MessageEvent, MessagePart, SessionSource
    from luna_agent.platforms.core import BasePlatformAdapter, ChatInfo, SendResult

    class Adapter(BasePlatformAdapter):
        def __init__(self):
            super().__init__(
                SimpleNamespace(agent_data_dir=tmp_path / "data", multimodal_image_mode="off"),
                db=None,
            )
            self.download_called = False

        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def send(self, chat_id: str, content: str) -> SendResult:
            return SendResult(success=True)

        async def get_chat_info(self, chat_id: str) -> ChatInfo:
            return ChatInfo(chat_id=chat_id)

        async def download_attachment(self, ref, source=None) -> DownloadedAttachment:
            self.download_called = True
            return DownloadedAttachment(data=b"payload", kind="image")

    adapter = Adapter()
    adapter.set_attachment_store(AttachmentStore(tmp_path / "cache"))
    event = MessageEvent(
        text="[image: file-1]",
        source=SessionSource(platform="test", user_id="u1", chat_id="c1"),
        attachments=[MessagePart(type="image", file_id="file-1", name="photo.png")],
        message_id="m1",
    )

    prepared = await adapter.prepare_inbound_attachments(event)
    attachment = prepared.envelope.attachments[0]

    assert adapter.download_called is False
    assert attachment.local_path == ""
    assert attachment.platform_file_id == "file-1"
    assert attachment.metadata["attachment_resolve"]["status"] == "skipped"
    assert attachment.metadata["attachment_resolve"]["reason"] == "mode_off"
