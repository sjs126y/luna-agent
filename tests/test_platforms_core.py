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
