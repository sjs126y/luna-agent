from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_agent.attachments import AttachmentStore
from personal_agent.conversation.input import ConversationInput
from personal_agent.models.messages import AttachmentRef
from personal_agent.multimodal import MultiAttachmentProcessor
from personal_agent.multimodal.image_text import (
    ImageTextCache,
    ImageTextDescribeUnavailable,
    ImageTextDescription,
    LocalOcrImageTextDescriber,
    VisionImageTextDescriber,
    build_default_image_text_describer,
)
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
        "multimodal_image_text_mode": "auto",
        "multimodal_image_text_cache": True,
        "multimodal_image_text_max_chars": 6000,
        "multimodal_image_text_provider": "",
        "multimodal_image_text_model": "",
        "multimodal_image_text_prompt": "",
        "multimodal_image_text_base_url": "",
        "multimodal_image_text_api_key": "k",
        "multimodal_ocr_endpoint": "",
        "multimodal_ocr_timeout_seconds": 20,
        "multimodal_ocr_language": "auto",
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
    assert result.diagnostics["reason_counts"] == {"file_not_found": 1}
    assert result.diagnostics["items"][0]["reason"] == "file_not_found"
    assert result.diagnostics["items"][0]["name"] == "missing.txt"
    assert result.diagnostics["items"][0]["has_local_path"] is False


@pytest.mark.asyncio
async def test_decrypt_key_unavailable_returns_clear_notice(tmp_path):
    processor = MultiAttachmentProcessor(
        settings=_settings(multimodal_image_mode="auto"),
        attachment_store=AttachmentStore(tmp_path / "cache"),
    )

    result = await processor.resolve(
        ConversationInput(
            text="describe",
            attachments=[
                AttachmentRef(
                    id="w1",
                    kind="image",
                    name="wechat.jpg",
                    metadata={
                        "attachment_resolve": {
                            "status": "failed",
                            "reason": "decrypt_key_unavailable",
                        }
                    },
                )
            ],
        ),
        provider=_provider(),
    )

    assert result.attachments[0].status == "failed"
    assert result.attachments[0].reason == "decrypt_key_unavailable"
    assert "微信加密图片缺少解密 key" in result.content_blocks[-1]["text"]
    assert result.diagnostics["reason_counts"] == {"decrypt_key_unavailable": 1}


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


@pytest.mark.asyncio
async def test_image_text_mode_uses_injected_describer(tmp_path):
    class Describer:
        def __init__(self):
            self.called = False

        async def describe(self, resolved, ref):
            self.called = True
            return ImageTextDescription(text="visible image text", method="fake", provider="test")

    source = tmp_path / "image.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\npayload")
    init_sandbox([tmp_path], [])
    describer = Describer()
    processor = MultiAttachmentProcessor(
        settings=_settings(multimodal_image_mode="text"),
        attachment_store=AttachmentStore(tmp_path / "cache"),
        image_text_describer=describer,
    )

    result = await processor.resolve(
        ConversationInput(
            text="read image",
            attachments=[AttachmentRef(id="a1", kind="image", name="image.png", local_path=str(source))],
        ),
        provider=_provider(supports_image=True),
    )

    assert describer.called is True
    assert result.attachments[0].status == "processed"
    assert result.attachments[0].reason == "image_text_described"
    assert "visible image text" in result.content_blocks[-1]["text"]
    assert "方法：fake" in result.content_blocks[-1]["text"]


@pytest.mark.asyncio
async def test_image_text_mode_defaults_to_notice_without_describer(tmp_path):
    source = tmp_path / "image.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\npayload")
    init_sandbox([tmp_path], [])
    processor = MultiAttachmentProcessor(
        settings=_settings(multimodal_image_mode="text"),
        attachment_store=AttachmentStore(tmp_path / "cache"),
    )

    result = await processor.resolve(
        ConversationInput(
            text="read image",
            attachments=[AttachmentRef(id="a1", kind="image", name="image.png", local_path=str(source))],
        ),
        provider=_provider(supports_image=True),
    )

    assert result.attachments[0].status == "unsupported"
    assert result.attachments[0].reason == "image_text_describer_unavailable"
    assert "当前没有可用的图片文本化能力" in result.content_blocks[-1]["text"]


@pytest.mark.asyncio
async def test_image_text_mode_off_returns_notice(tmp_path):
    class Describer:
        async def describe(self, resolved, ref):  # pragma: no cover - should never run
            raise AssertionError("describer should not be called")

    source = tmp_path / "image.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\npayload")
    init_sandbox([tmp_path], [])
    processor = MultiAttachmentProcessor(
        settings=_settings(multimodal_image_mode="text", multimodal_image_text_mode="off"),
        attachment_store=AttachmentStore(tmp_path / "cache"),
        image_text_describer=Describer(),
    )

    result = await processor.resolve(
        ConversationInput(
            text="read image",
            attachments=[AttachmentRef(id="a1", kind="image", name="image.png", local_path=str(source))],
        ),
        provider=_provider(supports_image=True),
    )

    assert result.attachments[0].status == "skipped"
    assert result.attachments[0].reason == "image_text_disabled"


@pytest.mark.asyncio
async def test_native_image_does_not_call_image_text_describer(tmp_path):
    class Describer:
        async def describe(self, resolved, ref):  # pragma: no cover - should never run
            raise AssertionError("describer should not be called")

    source = tmp_path / "image.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\npayload")
    init_sandbox([tmp_path], [])
    processor = MultiAttachmentProcessor(
        settings=_settings(multimodal_image_mode="auto"),
        attachment_store=AttachmentStore(tmp_path / "cache"),
        image_text_describer=Describer(),
    )

    result = await processor.resolve(
        ConversationInput(
            text="read image",
            attachments=[AttachmentRef(id="a1", kind="image", name="image.png", local_path=str(source))],
        ),
        provider=_provider(supports_image=True),
    )

    assert result.attachments[0].effective_mode == "native"


def test_image_text_cache_round_trips_by_identity(tmp_path):
    cache = ImageTextCache(tmp_path / "derived")
    first = ImageTextDescription(
        text="cached text",
        method="vision",
        provider="openai",
        model="vision-a",
        prompt_version=1,
        metadata={"x": 1},
    )

    cache.put(first, sha256="abc", source_mime_type="image/png")

    assert cache.get(
        sha256="abc",
        method="vision",
        provider="openai",
        model="vision-a",
        prompt_version=1,
    ).text == "cached text"
    assert cache.get(
        sha256="abc",
        method="vision",
        provider="openai",
        model="vision-b",
        prompt_version=1,
    ) is None


@pytest.mark.asyncio
async def test_vision_image_text_describer_calls_provider_and_caches(tmp_path):
    source = tmp_path / "image.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\npayload")
    init_sandbox([tmp_path], [])
    resolved = AttachmentStore(tmp_path / "cache").store_local_path(
        str(source),
        ref=AttachmentRef(id="a1", kind="image", name="image.png"),
    )
    calls = []

    async def call_fn(provider, transport, messages, max_tokens):
        calls.append((provider.name, provider.model, messages, max_tokens))
        return "vision text"

    describer = VisionImageTextDescriber(
        _settings(
            multimodal_image_text_provider="openai",
            multimodal_image_text_model="gpt-4o-mini",
            multimodal_image_text_base_url="https://api.openai.test/v1",
            multimodal_image_text_api_key="k",
        ),
        cache=ImageTextCache(tmp_path / "derived"),
        call_fn=call_fn,
    )

    first = await describer.describe(resolved, AttachmentRef(id="a1", kind="image", name="image.png"))
    second = await describer.describe(resolved, AttachmentRef(id="a1", kind="image", name="image.png"))

    assert first.text == "vision text"
    assert second.text == "vision text"
    assert second.cached is True
    assert len(calls) == 1
    assert calls[0][0] == "openai"
    assert calls[0][1] == "gpt-4o-mini"
    assert calls[0][2][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_vision_image_text_describer_requires_configured_provider(tmp_path):
    describer = VisionImageTextDescriber(_settings())

    with pytest.raises(ImageTextDescribeUnavailable) as exc:
        await describer.describe(
            resolved=type("Resolved", (), {
                "local_path": str(tmp_path / "missing.png"),
                "sha256": "",
                "mime_type": "image/png",
            })(),
            ref=AttachmentRef(id="a1", kind="image", name="missing.png"),
        )

    assert exc.value.reason == "image_text_describer_unavailable"


@pytest.mark.asyncio
async def test_vision_image_text_describer_rejects_non_image_provider(tmp_path):
    source = tmp_path / "image.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\npayload")
    init_sandbox([tmp_path], [])
    resolved = AttachmentStore(tmp_path / "cache").store_local_path(
        str(source),
        ref=AttachmentRef(id="a1", kind="image", name="image.png"),
    )
    describer = VisionImageTextDescriber(
        _settings(
            multimodal_image_text_provider="deepseek",
            multimodal_image_text_model="deepseek-chat",
        ),
    )

    with pytest.raises(ImageTextDescribeUnavailable) as exc:
        await describer.describe(resolved, AttachmentRef(id="a1", kind="image", name="image.png"))

    assert exc.value.reason == "image_text_provider_not_supported"


@pytest.mark.asyncio
async def test_image_text_processor_truncates_vision_result(tmp_path):
    class Describer:
        async def describe(self, resolved, ref):
            return ImageTextDescription(text="abcdef" * 20, method="vision")

    source = tmp_path / "image.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\npayload")
    init_sandbox([tmp_path], [])
    processor = MultiAttachmentProcessor(
        settings=_settings(
            multimodal_image_mode="text",
            multimodal_image_text_max_chars=24,
        ),
        attachment_store=AttachmentStore(tmp_path / "cache"),
        image_text_describer=Describer(),
    )

    result = await processor.resolve(
        ConversationInput(
            text="read image",
            attachments=[AttachmentRef(id="a1", kind="image", name="image.png", local_path=str(source))],
        ),
        provider=_provider(supports_image=True),
    )

    text = result.content_blocks[-1]["text"]
    assert "已截断，最多 24 字符" in text
    assert len(text.split("：\n", 1)[1]) <= 24


@pytest.mark.asyncio
async def test_local_ocr_image_text_describer_calls_service_and_caches(tmp_path):
    source = tmp_path / "image.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\npayload")
    init_sandbox([tmp_path], [])
    resolved = AttachmentStore(tmp_path / "cache").store_local_path(
        str(source),
        ref=AttachmentRef(id="a1", kind="image", name="image.png"),
    )
    calls = []

    async def http_fn(method, url, payload, timeout):
        calls.append((method, url, payload, timeout))
        if method == "GET":
            return {"ok": True, "engine": "fakeocr"}
        return {
            "ok": True,
            "text": "ocr text",
            "confidence": 0.9,
            "engine": "fakeocr",
            "blocks": [{"text": "ocr text", "confidence": 0.9, "bbox": [0, 0, 10, 10]}],
        }

    describer = LocalOcrImageTextDescriber(
        _settings(multimodal_ocr_endpoint="http://127.0.0.1:7788"),
        cache=ImageTextCache(tmp_path / "derived"),
        http_fn=http_fn,
    )

    first = await describer.describe(resolved, AttachmentRef(id="a1", kind="image", name="image.png"))
    second = await describer.describe(resolved, AttachmentRef(id="a1", kind="image", name="image.png"))

    assert first.text == "ocr text"
    assert second.cached is True
    assert len(calls) == 2
    assert calls[0][0] == "GET"
    assert calls[1][0] == "POST"
    assert calls[1][2]["image_path"].endswith(".png")
    assert Path(calls[1][2]["image_path"]).is_absolute()
    assert calls[1][2]["language"] == "auto"


@pytest.mark.asyncio
async def test_local_ocr_image_text_describer_handles_failed_response(tmp_path):
    source = tmp_path / "image.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\npayload")
    init_sandbox([tmp_path], [])
    resolved = AttachmentStore(tmp_path / "cache").store_local_path(
        str(source),
        ref=AttachmentRef(id="a1", kind="image", name="image.png"),
    )

    async def http_fn(method, url, payload, timeout):
        return {"ok": True} if method == "GET" else {"ok": False, "error": "unsupported_image"}

    describer = LocalOcrImageTextDescriber(
        _settings(multimodal_ocr_endpoint="http://127.0.0.1:7788"),
        http_fn=http_fn,
    )

    with pytest.raises(ImageTextDescribeUnavailable) as exc:
        await describer.describe(resolved, AttachmentRef(id="a1", kind="image", name="image.png"))

    assert exc.value.reason == "ocr_request_failed"


@pytest.mark.asyncio
async def test_local_ocr_image_text_describer_handles_empty_text(tmp_path):
    source = tmp_path / "image.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\npayload")
    init_sandbox([tmp_path], [])
    resolved = AttachmentStore(tmp_path / "cache").store_local_path(
        str(source),
        ref=AttachmentRef(id="a1", kind="image", name="image.png"),
    )

    async def http_fn(method, url, payload, timeout):
        return {"ok": True} if method == "GET" else {"ok": True, "text": ""}

    describer = LocalOcrImageTextDescriber(
        _settings(multimodal_ocr_endpoint="http://127.0.0.1:7788"),
        http_fn=http_fn,
    )

    with pytest.raises(ImageTextDescribeUnavailable) as exc:
        await describer.describe(resolved, AttachmentRef(id="a1", kind="image", name="image.png"))

    assert exc.value.reason == "ocr_empty"


@pytest.mark.asyncio
async def test_local_ocr_image_text_describer_handles_invalid_response(tmp_path):
    source = tmp_path / "image.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\npayload")
    init_sandbox([tmp_path], [])
    resolved = AttachmentStore(tmp_path / "cache").store_local_path(
        str(source),
        ref=AttachmentRef(id="a1", kind="image", name="image.png"),
    )

    async def http_fn(method, url, payload, timeout):
        return "bad"

    describer = LocalOcrImageTextDescriber(
        _settings(multimodal_ocr_endpoint="http://127.0.0.1:7788"),
        http_fn=http_fn,
    )

    with pytest.raises(ImageTextDescribeUnavailable) as exc:
        await describer.describe(resolved, AttachmentRef(id="a1", kind="image", name="image.png"))

    assert exc.value.reason == "ocr_response_invalid"


def test_default_image_text_describer_auto_uses_ocr_without_vision(tmp_path):
    describer = build_default_image_text_describer(
        _settings(
            multimodal_image_text_mode="auto",
            multimodal_image_text_provider="",
            multimodal_ocr_endpoint="http://127.0.0.1:7788",
        ),
        AttachmentStore(tmp_path / "cache"),
    )

    assert isinstance(describer, LocalOcrImageTextDescriber)


def test_default_image_text_describer_vision_mode_does_not_use_ocr(tmp_path):
    describer = build_default_image_text_describer(
        _settings(
            multimodal_image_text_mode="vision",
            multimodal_image_text_provider="",
            multimodal_ocr_endpoint="http://127.0.0.1:7788",
        ),
        AttachmentStore(tmp_path / "cache"),
    )

    assert not isinstance(describer, LocalOcrImageTextDescriber)
