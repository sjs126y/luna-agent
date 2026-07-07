from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_agent.attachments import AttachmentStore
from personal_agent.conversation.input import ConversationInput
from personal_agent.models.messages import AttachmentRef
from personal_agent.multimodal import MultiAttachmentProcessor
from personal_agent.tools.sandbox import init_sandbox


def _settings(**overrides):
    values = {
        "multimodal_enabled": True,
        "multimodal_image_mode": "auto",
        "multimodal_audio_mode": "auto",
        "multimodal_video_mode": "off",
        "multimodal_file_mode": "auto",
        "multimodal_native_fallback": "notice",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _provider(supports_image=False):
    return SimpleNamespace(
        supports_image_input=supports_image,
        supported_image_mime_types=("image/png", "image/jpeg"),
        max_image_bytes=20 * 1024 * 1024,
    )


@pytest.mark.asyncio
async def test_off_mode_does_not_resolve_attachment(tmp_path):
    class Store:
        def resolve(self, ref):  # pragma: no cover - should never run
            raise AssertionError("store should not be called")

    processor = MultiAttachmentProcessor(
        settings=_settings(multimodal_image_mode="off"),
        attachment_store=Store(),
    )
    result = await processor.resolve(
        ConversationInput(
            text="hello",
            attachments=[AttachmentRef(id="a1", kind="image", name="x.png", local_path="/tmp/x.png")],
        ),
        provider=_provider(supports_image=True),
    )

    assert result.text == "hello"
    assert result.attachments[0].status == "skipped"
    assert result.attachments[0].reason == "mode_off"
    assert result.diagnostics["notice_count"] == 1


@pytest.mark.asyncio
async def test_native_image_creates_image_url_block(tmp_path):
    source = tmp_path / "image.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\npayload")
    init_sandbox([tmp_path], [])
    processor = MultiAttachmentProcessor(
        settings=_settings(multimodal_image_mode="native"),
        attachment_store=AttachmentStore(tmp_path / "cache"),
    )

    result = await processor.resolve(
        ConversationInput(
            text="describe",
            attachments=[AttachmentRef(id="a1", kind="image", name="image.png", local_path=str(source))],
        ),
        provider=_provider(supports_image=True),
    )

    assert result.attachments[0].status == "processed"
    assert result.attachments[0].effective_mode == "native"
    assert any(block.get("type") == "image_url" for block in result.content_blocks)
    assert result.diagnostics["native_count"] == 1


@pytest.mark.asyncio
async def test_text_mode_falls_back_to_notice_when_describer_missing(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("hello", encoding="utf-8")
    init_sandbox([tmp_path], [])
    processor = MultiAttachmentProcessor(
        settings=_settings(multimodal_file_mode="text"),
        attachment_store=AttachmentStore(tmp_path / "cache"),
    )

    result = await processor.resolve(
        ConversationInput(
            text="read",
            attachments=[AttachmentRef(id="f1", kind="file", name="note.txt", local_path=str(source))],
        ),
        provider=_provider(),
    )

    assert result.attachments[0].effective_mode == "text"
    assert result.attachments[0].reason == "describer_unavailable"
    assert "当前没有可用的文本化能力" in result.content_blocks[-1]["text"]


@pytest.mark.asyncio
async def test_native_image_respects_provider_mime_capability(tmp_path):
    source = tmp_path / "image.gif"
    source.write_bytes(b"GIF89apayload")
    init_sandbox([tmp_path], [])
    processor = MultiAttachmentProcessor(
        settings=_settings(multimodal_image_mode="native"),
        attachment_store=AttachmentStore(tmp_path / "cache"),
    )

    result = await processor.resolve(
        ConversationInput(
            text="describe",
            attachments=[AttachmentRef(id="a1", kind="image", name="image.gif", local_path=str(source))],
        ),
        provider=_provider(supports_image=True),
    )

    assert result.attachments[0].status == "unsupported"
    assert result.attachments[0].reason == "mime_type_not_supported"
    assert not any(block.get("type") == "image_url" for block in result.content_blocks)
