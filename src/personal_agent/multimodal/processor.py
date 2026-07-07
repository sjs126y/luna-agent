"""Fault-tolerant multimodal attachment processing."""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from personal_agent.attachments.store import AttachmentStore, AttachmentStoreError, ResolvedAttachment
from personal_agent.attachments.text_extract import AttachmentTextExtractError, extract_attachment_text
from personal_agent.conversation.input import ConversationInput
from personal_agent.models.messages import AttachmentRef
from personal_agent.multimodal.image_text import (
    ImageTextDescribeUnavailable,
    ImageTextDescriber,
    NullImageTextDescriber,
)
from personal_agent.text_safety import clean_text


@dataclass
class ProcessedAttachment:
    id: str
    kind: str
    configured_mode: str
    effective_mode: str
    status: str
    reason: str = ""
    notice_text: str = ""
    summary_text: str = ""
    error: str = ""
    ref: AttachmentRef | None = None
    resolved: ResolvedAttachment | None = None
    content_blocks: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.ref is not None:
            data["ref"] = self.ref.as_dict()
        if self.resolved is not None:
            data["resolved"] = self.resolved.as_dict()
        return data


@dataclass
class ResolvedConversationInput:
    text: str
    content_blocks: list[dict[str, Any]]
    attachments: list[ProcessedAttachment] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    original: ConversationInput | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "content_blocks": list(self.content_blocks),
            "attachments": [item.as_dict() for item in self.attachments],
            "diagnostics": dict(self.diagnostics),
            "original": {
                "text": self.original.text,
                "attachment_count": len(self.original.attachments),
                "attachment_kinds": self.original.attachment_kinds(),
            } if self.original is not None else None,
        }


class AttachmentTextDescriber(Protocol):
    async def describe(self, resolved: ResolvedAttachment, ref: AttachmentRef) -> str:
        ...


class NullAttachmentTextDescriber:
    async def describe(self, resolved: ResolvedAttachment, ref: AttachmentRef) -> str:
        raise AttachmentDescribeUnavailable("describer_unavailable")


class AttachmentDescribeUnavailable(RuntimeError):
    def __init__(self, reason: str = "describer_unavailable", detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail or reason)


class LocalAttachmentTextDescriber:
    def __init__(self, settings) -> None:
        self.settings = settings

    async def describe(self, resolved: ResolvedAttachment, ref: AttachmentRef) -> str:
        if not resolved.local_path:
            raise AttachmentDescribeUnavailable("attachment_not_resolved")
        max_chars = int(getattr(self.settings, "multimodal_text_extract_max_chars", 12000) or 12000)
        pdf_max_pages = int(getattr(self.settings, "multimodal_text_extract_pdf_max_pages", 20) or 20)
        try:
            extracted = extract_attachment_text(
                resolved.local_path,
                mime_type=resolved.mime_type,
                name=resolved.name or ref.name,
                max_chars=max_chars,
                pdf_max_pages=pdf_max_pages,
            )
        except AttachmentTextExtractError as exc:
            raise AttachmentDescribeUnavailable(exc.reason, exc.detail) from exc
        except Exception as exc:
            raise AttachmentDescribeUnavailable("text_extract_failed", f"{type(exc).__name__}: {exc}") from exc

        label = resolved.name or ref.name or resolved.local_path or resolved.id
        if extracted.truncated:
            header = f"附件 {label} 内容摘录（已截断，最多 {max_chars} 字符）："
        elif extracted.source_type in {"pdf", "docx"}:
            header = f"附件 {label} 文本摘录："
        else:
            header = f"附件 {label} 内容："
        return f"{header}\n{extracted.text}"


class MultiAttachmentProcessor:
    def __init__(
        self,
        *,
        settings,
        attachment_store: AttachmentStore | None = None,
        text_describer: AttachmentTextDescriber | None = None,
        image_text_describer: ImageTextDescriber | None = None,
    ) -> None:
        self.settings = settings
        self.attachment_store = attachment_store
        self.text_describer = text_describer or LocalAttachmentTextDescriber(settings)
        self.image_text_describer = image_text_describer or NullImageTextDescriber()

    async def resolve(
        self,
        user_input: ConversationInput,
        *,
        provider,
    ) -> ResolvedConversationInput:
        text = clean_text(user_input.text or "")
        attachments = list(user_input.attachments or [])
        processed: list[ProcessedAttachment] = []
        content_blocks: list[dict[str, Any]] = []
        if text:
            content_blocks.append({"type": "text", "text": text})

        if not attachments:
            return ResolvedConversationInput(
                text=text,
                content_blocks=content_blocks or [{"type": "text", "text": text}],
                attachments=[],
                diagnostics=_diagnostics([], settings=self.settings),
                original=user_input,
            )

        for ref in attachments:
            item = await self._process_one(ref, provider=provider)
            processed.append(item)
            if item.summary_text:
                content_blocks.append({"type": "text", "text": item.summary_text})
            elif item.notice_text:
                content_blocks.append({"type": "text", "text": item.notice_text})
            content_blocks.extend(item.content_blocks)

        if not content_blocks:
            content_blocks.append({"type": "text", "text": text})
        return ResolvedConversationInput(
            text=text,
            content_blocks=content_blocks,
            attachments=processed,
            diagnostics=_diagnostics(processed, settings=self.settings),
            original=user_input,
        )

    async def _process_one(self, ref: AttachmentRef, *, provider) -> ProcessedAttachment:
        kind = _canonical_kind(ref)
        configured_mode = _configured_mode(self.settings, kind)
        attachment_id = ref.id or ref.platform_file_id or ref.local_path or ref.url or ref.name or kind

        if not bool(getattr(self.settings, "multimodal_enabled", True)):
            return _notice(ref, attachment_id, kind, configured_mode, "off", "skipped",
                           "multimodal_disabled")
        if configured_mode == "off":
            return _notice(ref, attachment_id, kind, configured_mode, "off", "skipped", "mode_off")

        prepared_status = _prepared_resolution_status(ref)
        if prepared_status and prepared_status.get("status") in {"skipped", "failed"}:
            status = "failed" if prepared_status.get("status") == "failed" else "skipped"
            return _notice(
                ref,
                attachment_id,
                kind,
                configured_mode,
                "notice",
                status,
                str(prepared_status.get("reason") or "attachment_not_resolved"),
                error=str(prepared_status.get("error") or ""),
            )

        effective_mode = _effective_mode(
            configured_mode,
            kind=kind,
            provider=provider,
            settings=self.settings,
        )

        store = self.attachment_store
        if store is None:
            return _notice(ref, attachment_id, kind, configured_mode, "notice", "failed",
                           "attachment_store_unavailable")
        try:
            resolved = store.resolve(ref)
        except AttachmentStoreError as exc:
            return _notice(
                ref,
                attachment_id,
                kind,
                configured_mode,
                effective_mode,
                "failed",
                exc.reason,
                error=str(exc),
            )
        except Exception as exc:
            return _notice(
                ref,
                attachment_id,
                kind,
                configured_mode,
                effective_mode,
                "failed",
                "resolve_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        if effective_mode == "notice":
            return _notice(
                ref,
                resolved.id,
                kind,
                configured_mode,
                effective_mode,
                "unsupported",
                "provider_not_supported",
                resolved=resolved,
            )

        if effective_mode == "text" and kind == "image":
            return await self._describe_image_text(ref, resolved, configured_mode=configured_mode)
        if effective_mode == "text":
            return await self._describe_text(ref, resolved, configured_mode=configured_mode)
        if effective_mode == "native":
            unsupported_reason = _provider_image_unsupported_reason(resolved, provider)
            if unsupported_reason:
                fallback = str(getattr(self.settings, "multimodal_native_fallback", "notice") or "notice")
                if fallback == "text":
                    return await self._describe_text(ref, resolved, configured_mode=configured_mode)
                return _notice(ref, resolved.id, kind, configured_mode, "native", "unsupported",
                               unsupported_reason, resolved=resolved)
            native = _native_image_block(resolved)
            if native is None:
                fallback = str(getattr(self.settings, "multimodal_native_fallback", "notice") or "notice")
                if fallback == "text":
                    return await self._describe_text(ref, resolved, configured_mode=configured_mode)
                return _notice(ref, resolved.id, kind, configured_mode, "native", "unsupported",
                               "native_block_unavailable", resolved=resolved)
            return ProcessedAttachment(
                id=resolved.id,
                kind=kind,
                configured_mode=configured_mode,
                effective_mode="native",
                status="processed",
                reason="native_image",
                ref=ref,
                resolved=resolved,
                content_blocks=[native],
                metadata={"mime_type": resolved.mime_type, "size": resolved.size},
            )

        return _notice(ref, attachment_id, kind, configured_mode, effective_mode, "unsupported",
                       "unsupported_mode")

    async def _describe_image_text(
        self,
        ref: AttachmentRef,
        resolved: ResolvedAttachment,
        *,
        configured_mode: str,
    ) -> ProcessedAttachment:
        image_text_mode = str(getattr(self.settings, "multimodal_image_text_mode", "auto") or "auto").lower()
        if image_text_mode == "off":
            return _notice(
                ref,
                resolved.id,
                "image",
                configured_mode,
                "text",
                "skipped",
                "image_text_disabled",
                resolved=resolved,
            )
        try:
            description = await self.image_text_describer.describe(resolved, ref)
        except ImageTextDescribeUnavailable as exc:
            return _notice(
                ref,
                resolved.id,
                "image",
                configured_mode,
                "text",
                "unsupported" if exc.reason == "image_text_describer_unavailable" else "failed",
                exc.reason or "image_text_describer_unavailable",
                error=exc.detail or str(exc),
                resolved=resolved,
            )
        except Exception as exc:
            return _notice(
                ref,
                resolved.id,
                "image",
                configured_mode,
                "text",
                "failed",
                "image_text_failed",
                error=f"{type(exc).__name__}: {exc}",
                resolved=resolved,
            )
        text = clean_text(description.text or "")
        max_chars = int(getattr(self.settings, "multimodal_image_text_max_chars", 6000) or 6000)
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars].rstrip()
        if not text:
            return _notice(
                ref,
                resolved.id,
                "image",
                configured_mode,
                "text",
                "failed",
                "image_text_empty",
                resolved=resolved,
            )
        label = resolved.name or ref.name or resolved.local_path or resolved.id
        header = f"附件 {label} 图片文本化结果"
        if description.method and description.method != "unknown":
            header += f"（方法：{description.method}）"
        if truncated:
            header += f"（已截断，最多 {max_chars} 字符）"
        summary = f"{header}：\n{text}"
        return ProcessedAttachment(
            id=resolved.id,
            kind="image",
            configured_mode=configured_mode,
            effective_mode="text",
            status="processed",
            reason="image_text_described",
            summary_text=summary,
            ref=ref,
            resolved=resolved,
            metadata={
                "method": description.method,
                "provider": description.provider,
                "model": description.model,
                "cached": description.cached,
                "confidence": description.confidence,
                **dict(description.metadata),
            },
        )

    async def _describe_text(
        self,
        ref: AttachmentRef,
        resolved: ResolvedAttachment,
        *,
        configured_mode: str,
    ) -> ProcessedAttachment:
        try:
            summary = await self.text_describer.describe(resolved, ref)
        except AttachmentDescribeUnavailable as exc:
            reason = exc.reason or "describer_unavailable"
            return _notice(
                ref,
                resolved.id,
                resolved.kind,
                configured_mode,
                "text",
                "unsupported" if reason in {"unsupported_file_type", "describer_unavailable"} else "failed",
                reason,
                error=exc.detail or str(exc),
                resolved=resolved,
            )
        except Exception as exc:
            return _notice(
                ref,
                resolved.id,
                resolved.kind,
                configured_mode,
                "text",
                "unsupported",
                "describer_unavailable",
                error=f"{type(exc).__name__}: {exc}",
                resolved=resolved,
            )
        summary = clean_text(summary or "")
        if not summary:
            return _notice(ref, resolved.id, resolved.kind, configured_mode, "text", "failed",
                           "empty_description", resolved=resolved)
        return ProcessedAttachment(
            id=resolved.id,
            kind=resolved.kind,
            configured_mode=configured_mode,
            effective_mode="text",
            status="processed",
            reason="described",
            summary_text=summary,
            ref=ref,
            resolved=resolved,
        )


def _configured_mode(settings, kind: str) -> str:
    attr = {
        "image": "multimodal_image_mode",
        "audio": "multimodal_audio_mode",
        "video": "multimodal_video_mode",
        "file": "multimodal_file_mode",
    }.get(kind, "multimodal_file_mode")
    value = str(getattr(settings, attr, "auto") or "auto").strip().lower()
    if value not in {"auto", "native", "text", "off"}:
        return "auto"
    if kind != "image" and value == "native":
        return "auto"
    return value


def _effective_mode(mode: str, *, kind: str, provider, settings) -> str:
    supports_native = kind == "image" and bool(getattr(provider, "supports_image_input", False))
    if mode == "text":
        return "text"
    if mode == "native":
        if supports_native:
            return "native"
        return "text" if getattr(settings, "multimodal_native_fallback", "notice") == "text" else "notice"
    if mode == "auto":
        if supports_native:
            return "native"
        if kind in {"image", "audio", "file"}:
            return "text"
        return "notice"
    return "notice"


def _native_image_block(resolved: ResolvedAttachment) -> dict[str, Any] | None:
    if resolved.kind != "image":
        return None
    path = Path(resolved.local_path)
    if not path.exists():
        return None
    mime_type = resolved.mime_type or "application/octet-stream"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{data}"}}


def _provider_image_unsupported_reason(resolved: ResolvedAttachment, provider) -> str:
    if not bool(getattr(provider, "supports_image_input", False)):
        return "provider_not_supported"
    supported_mime = tuple(getattr(provider, "supported_image_mime_types", ()) or ())
    if supported_mime and resolved.mime_type and resolved.mime_type not in supported_mime:
        return "mime_type_not_supported"
    max_bytes = int(getattr(provider, "max_image_bytes", 0) or 0)
    if max_bytes and resolved.size > max_bytes:
        return "size_exceeded"
    return ""


def _notice(
    ref: AttachmentRef,
    attachment_id: str,
    kind: str,
    configured_mode: str,
    effective_mode: str,
    status: str,
    reason: str,
    *,
    error: str = "",
    resolved: ResolvedAttachment | None = None,
) -> ProcessedAttachment:
    label = ref.name or ref.local_path or ref.url or ref.platform_file_id or attachment_id
    text = _notice_text(kind, label, reason)
    return ProcessedAttachment(
        id=attachment_id,
        kind=kind,
        configured_mode=configured_mode,
        effective_mode=effective_mode,
        status=status,
        reason=reason,
        notice_text=text,
        error=error,
        ref=ref,
        resolved=resolved,
    )


def _notice_text(kind: str, label: str, reason: str) -> str:
    kind_label = {"image": "图片", "audio": "音频", "video": "视频", "file": "文件"}.get(kind, "附件")
    reason_label = {
        "mode_off": "当前配置关闭了该类型附件处理，未下载、未解析、未传给模型。",
        "multimodal_disabled": "当前配置关闭了多模态处理，未下载、未解析、未传给模型。",
        "resolve_inbound_disabled": "当前配置关闭了平台附件本地化。",
        "cache_disabled": "当前配置关闭了平台附件缓存。",
        "url_download_disabled": "当前配置关闭了附件 URL 下载。",
        "platform_download_disabled": "当前配置关闭了平台文件下载。",
        "provider_not_supported": "当前 provider 不支持原生处理，且没有可用的文本化能力。",
        "describer_unavailable": "当前没有可用的文本化能力。",
        "image_text_disabled": "当前配置关闭了图片文本化。",
        "image_text_describer_unavailable": "当前没有可用的图片文本化能力。",
        "image_text_failed": "图片文本化失败。",
        "image_text_empty": "图片文本化没有返回有效内容。",
        "text_extract_unavailable": "当前文本抽取能力不可用。",
        "text_extract_failed": "附件文本抽取失败。",
        "unsupported_file_type": "当前不支持该文件类型的文本抽取。",
        "empty_description": "附件没有可抽取的文本内容。",
        "path_not_allowed": "本地路径不在允许范围内。",
        "unsafe_url": "附件 URL 未通过安全检查。",
        "size_exceeded": "附件超过大小限制。",
        "platform_file_download_unavailable": "该平台文件暂时没有通用下载能力。",
        "platform_download_unavailable": "该平台文件暂时没有下载能力。",
        "attachment_has_no_resolvable_location": "附件没有可下载或可读取的位置。",
        "attachment_not_resolved": "附件尚未本地化。",
        "attachment_store_unavailable": "附件缓存服务不可用。",
        "mime_type_not_supported": "当前 provider 不支持该图片格式。",
        "resolve_failed": "附件本地化失败。",
    }.get(reason, "当前无法处理该附件。")
    return f"已收到{kind_label} {label}，但{reason_label}"


def _canonical_kind(ref: AttachmentRef) -> str:
    value = str(ref.kind or "").lower()
    if value in {"image", "photo", "picture"}:
        return "image"
    if value in {"audio", "voice"}:
        return "audio"
    if value == "video":
        return "video"
    return "file"


def _prepared_resolution_status(ref: AttachmentRef) -> dict[str, Any]:
    value = dict(ref.metadata or {}).get("attachment_resolve")
    return dict(value) if isinstance(value, dict) else {}


def _diagnostics(items: list[ProcessedAttachment], *, settings) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    mode_counts: dict[str, int] = {}
    kind_counts: dict[str, int] = {}
    for item in items:
        status_counts[item.status] = status_counts.get(item.status, 0) + 1
        mode_counts[item.effective_mode] = mode_counts.get(item.effective_mode, 0) + 1
        kind_counts[item.kind] = kind_counts.get(item.kind, 0) + 1
    return {
        "enabled": bool(getattr(settings, "multimodal_enabled", True)),
        "attachments_count": len(items),
        "attachment_kinds": sorted(kind_counts),
        "status_counts": dict(sorted(status_counts.items())),
        "effective_modes": dict(sorted(mode_counts.items())),
        "resolved_count": sum(1 for item in items if item.resolved is not None),
        "native_count": sum(1 for item in items if item.effective_mode == "native"),
        "notice_count": sum(1 for item in items if item.notice_text),
        "failed_count": sum(1 for item in items if item.status == "failed"),
    }
