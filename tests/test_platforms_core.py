"""Platform runtime import boundaries."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_platform_core_is_public_import_path():
    from personal_agent.platforms.core import (
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
    from personal_agent.models.messages import MessageEvent, MessagePart, SessionSource

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
    from personal_agent.models.messages import MessagePart, ResponseEnvelope

    response = ResponseEnvelope(parts=[
        MessagePart(type="text", text="file "),
        MessagePart(type="file", name="report.pdf"),
    ])

    assert response.render_text() == "file [file: report.pdf]"
    assert response.as_dict()["parts"][1]["type"] == "file"


@pytest.mark.asyncio
async def test_base_adapter_send_message_falls_back_to_text():
    from personal_agent.models.messages import MessagePart, OutboundMessage
    from personal_agent.platforms.core import BasePlatformAdapter, ChatInfo, SendResult

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
