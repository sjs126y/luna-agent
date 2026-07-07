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
        "multimodal_text_extract_max_chars": 12000,
        "multimodal_text_extract_pdf_max_pages": 20,
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
async def test_prepared_skip_does_not_resolve_attachment(tmp_path):
    class Store:
        def resolve(self, ref):  # pragma: no cover - should never run
            raise AssertionError("store should not be called")

    processor = MultiAttachmentProcessor(
        settings=_settings(multimodal_image_mode="auto"),
        attachment_store=Store(),
    )
    result = await processor.resolve(
        ConversationInput(
            text="hello",
            attachments=[
                AttachmentRef(
                    id="a1",
                    kind="image",
                    name="x.png",
                    platform_file_id="file-1",
                    metadata={
                        "attachment_resolve": {
                            "status": "skipped",
                            "reason": "platform_download_disabled",
                        },
                    },
                )
            ],
        ),
        provider=_provider(supports_image=True),
    )

    assert result.attachments[0].status == "skipped"
    assert result.attachments[0].reason == "platform_download_disabled"
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
async def test_provider_unsupported_does_not_block_local_resolution(tmp_path):
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
        provider=_provider(supports_image=False),
    )

    assert result.attachments[0].status == "unsupported"
    assert result.attachments[0].reason == "provider_not_supported"
    assert result.attachments[0].resolved is not None
    assert result.diagnostics["resolved_count"] == 1


@pytest.mark.asyncio
async def test_text_mode_extracts_plain_text_file(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("hello from attachment", encoding="utf-8")
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
    assert result.attachments[0].status == "processed"
    assert result.attachments[0].reason == "described"
    assert "附件 note.txt 内容" in result.content_blocks[-1]["text"]
    assert "hello from attachment" in result.content_blocks[-1]["text"]


@pytest.mark.asyncio
async def test_text_mode_truncates_long_file(tmp_path):
    source = tmp_path / "app.log"
    source.write_text("abcdef" * 20, encoding="utf-8")
    init_sandbox([tmp_path], [])
    processor = MultiAttachmentProcessor(
        settings=_settings(
            multimodal_file_mode="text",
            multimodal_text_extract_max_chars=30,
        ),
        attachment_store=AttachmentStore(tmp_path / "cache"),
    )

    result = await processor.resolve(
        ConversationInput(
            text="read",
            attachments=[AttachmentRef(id="f1", kind="file", name="app.log", local_path=str(source))],
        ),
        provider=_provider(),
    )

    text = result.content_blocks[-1]["text"]
    assert result.attachments[0].status == "processed"
    assert "已截断，最多 30 字符" in text
    assert len(text.split("：\n", 1)[1]) <= 30


@pytest.mark.asyncio
async def test_text_mode_extracts_pdf(tmp_path):
    import fitz

    source = tmp_path / "report.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "pdf attachment text")
    doc.save(str(source))
    doc.close()
    init_sandbox([tmp_path], [])
    processor = MultiAttachmentProcessor(
        settings=_settings(multimodal_file_mode="text"),
        attachment_store=AttachmentStore(tmp_path / "cache"),
    )

    result = await processor.resolve(
        ConversationInput(
            text="read",
            attachments=[AttachmentRef(id="f1", kind="file", name="report.pdf", local_path=str(source))],
        ),
        provider=_provider(),
    )

    assert result.attachments[0].status == "processed"
    assert "附件 report.pdf 文本摘录" in result.content_blocks[-1]["text"]
    assert "pdf attachment text" in result.content_blocks[-1]["text"]


@pytest.mark.asyncio
async def test_text_mode_extracts_docx(tmp_path):
    from docx import Document

    source = tmp_path / "report.docx"
    doc = Document()
    doc.add_paragraph("docx attachment text")
    doc.save(str(source))
    init_sandbox([tmp_path], [])
    processor = MultiAttachmentProcessor(
        settings=_settings(multimodal_file_mode="text"),
        attachment_store=AttachmentStore(tmp_path / "cache"),
    )

    result = await processor.resolve(
        ConversationInput(
            text="read",
            attachments=[AttachmentRef(id="f1", kind="file", name="report.docx", local_path=str(source))],
        ),
        provider=_provider(),
    )

    assert result.attachments[0].status == "processed"
    assert "附件 report.docx 文本摘录" in result.content_blocks[-1]["text"]
    assert "docx attachment text" in result.content_blocks[-1]["text"]


@pytest.mark.asyncio
async def test_text_mode_unsupported_binary_file_returns_notice(tmp_path):
    source = tmp_path / "blob.bin"
    source.write_bytes(b"\x00\x01\x02\x03")
    init_sandbox([tmp_path], [])
    processor = MultiAttachmentProcessor(
        settings=_settings(multimodal_file_mode="text"),
        attachment_store=AttachmentStore(tmp_path / "cache"),
    )

    result = await processor.resolve(
        ConversationInput(
            text="read",
            attachments=[AttachmentRef(id="f1", kind="file", name="blob.bin", local_path=str(source))],
        ),
        provider=_provider(),
    )

    assert result.attachments[0].effective_mode == "text"
    assert result.attachments[0].status == "unsupported"
    assert result.attachments[0].reason == "unsupported_file_type"
    assert "当前不支持该文件类型的文本抽取" in result.content_blocks[-1]["text"]


@pytest.mark.asyncio
async def test_store_resolution_error_returns_notice(tmp_path):
    from personal_agent.attachments.store import AttachmentStoreError

    class Store:
        def resolve(self, ref):
            raise AttachmentStoreError("file_not_found")

    processor = MultiAttachmentProcessor(
        settings=_settings(multimodal_file_mode="text"),
        attachment_store=Store(),
    )

    result = await processor.resolve(
        ConversationInput(
            text="read",
            attachments=[AttachmentRef(id="f1", kind="file", name="missing.txt", local_path=str(tmp_path / "missing.txt"))],
        ),
        provider=_provider(),
    )

    assert result.attachments[0].status == "failed"
    assert result.attachments[0].reason == "file_not_found"
    assert result.diagnostics["failed_count"] == 1


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
