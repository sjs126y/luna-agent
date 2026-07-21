"""Platform core — base adapter, message pipeline, and registry."""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Callable, Awaitable
from dataclasses import dataclass, field
from pathlib import Path

from luna_agent.attachments import AttachmentStore, DownloadedAttachment, ResolvedAttachment
from luna_agent.attachments.store import AttachmentStoreError
from luna_agent.models.messages import AttachmentRef, MessageEvent, OutboundMessage, PlatformCapabilities
from luna_agent.models.messages import SessionSource
from luna_agent.platforms.hooks import AdapterHooks
from luna_agent.platforms.setup import PlatformSetupContext, PlatformSetupResult

logger = logging.getLogger(__name__)

MESSAGE_DEDUPE_MAXSIZE = 1024


# ── result types ─────────────────────────────────────

@dataclass
class SendResult:
    success: bool
    message_id: str | None = None
    error: str | None = None


@dataclass
class ChatInfo:
    chat_id: str
    chat_type: str = "dm"
    chat_name: str = ""
    member_count: int = 0


@dataclass
class SendStats:
    sent_count: int = 0
    failed_count: int = 0
    retry_count: int = 0
    last_send_at: str = ""
    last_success_at: str = ""
    last_error_at: str = ""
    last_error: str = ""

    def snapshot(self) -> dict:
        return {
            "sent_count": self.sent_count,
            "failed_count": self.failed_count,
            "retry_count": self.retry_count,
            "last_send_at": self.last_send_at,
            "last_success_at": self.last_success_at,
            "last_error_at": self.last_error_at,
            "last_error": self.last_error,
        }


class AttachmentDownloadError(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail or reason)


# ── PlatformEntry & PlatformRegistry ─────────────────

@dataclass
class PlatformEntry:
    name: str
    factory: Callable[..., "BasePlatformAdapter"]
    check_fn: Callable[[object], bool]  # config → bool
    capabilities: PlatformCapabilities = field(default_factory=PlatformCapabilities)
    setup_fn: Callable[[PlatformSetupContext], Awaitable[PlatformSetupResult] | PlatformSetupResult] | None = None


class PlatformRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, PlatformEntry] = {}

    def register(self, entry: PlatformEntry) -> None:
        self._entries[entry.name] = entry

    def unregister(self, name: str) -> None:
        self._entries.pop(name, None)

    def get(self, name: str) -> PlatformEntry | None:
        return self._entries.get(name)

    def list(self) -> list[PlatformEntry]:
        return list(self._entries.values())

    def create_adapter(self, name: str, config, db) -> BasePlatformAdapter:
        return self._entries[name].factory(config, db)

    def is_available(self, name: str, config) -> bool:
        entry = self._entries.get(name)
        return entry is not None and entry.check_fn(config)


platform_registry = PlatformRegistry()


def _int_setting(value, default: int, *, minimum: int = 0) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    if result < minimum:
        return default
    return result


# ── BasePlatformAdapter ──────────────────────────────

class BasePlatformAdapter(ABC):
    """Platform transport boundary for inbound events and outbound messages."""

    capabilities = PlatformCapabilities()

    def __init__(self, config, db) -> None:
        self.config = config
        self.db = db
        self._loop: asyncio.AbstractEventLoop | None = None
        self._message_handler: Callable[[MessageEvent], Awaitable[str | None]] | None = None
        self._seen_message_keys: OrderedDict[str, None] = OrderedDict()
        self._dedupe_max_size = _int_setting(
            getattr(config, "platform_message_dedupe_max_size", MESSAGE_DEDUPE_MAXSIZE),
            MESSAGE_DEDUPE_MAXSIZE,
            minimum=1,
        )
        self.hooks = AdapterHooks()
        self._platform_name = type(self).__name__
        self._connected = False
        self._last_connected_at = ""
        self._last_disconnected_at = ""
        self._last_connect_error = ""
        self._last_send_error = ""
        self._last_message_at = ""
        self._last_response_at = ""
        self._send_stats = SendStats()
        self._attachment_store: AttachmentStore | None = None

    # ── abstract methods (subclass MUST implement) ───

    @abstractmethod
    async def connect(self) -> None:
        """Start connection. MUST capture self._loop = asyncio.get_running_loop()."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection. Idempotent — repeat calls safe."""

    @abstractmethod
    async def send(self, chat_id: str, content: str) -> SendResult:
        """Send message to chat. Subclass handles platform-specific formatting."""

    async def send_message(self, chat_id: str, message: OutboundMessage) -> SendResult:
        """Encode and send one logical message without application-level retries."""
        chunks = split_text_for_platform(message.render_text(), self._max_outbound_text_length())
        last_result = SendResult(success=True)
        for index, chunk in enumerate(chunks):
            self._send_stats.last_send_at = _now()
            try:
                result = await self.send(chat_id, chunk)
            except Exception as exc:
                self._last_send_error = f"{type(exc).__name__}: {exc}"
                self._record_send_failure(self._last_send_error)
                if index:
                    return SendResult(
                        success=False,
                        error=f"partial delivery: {self._last_send_error}",
                    )
                raise
            if not result.success:
                self._last_send_error = result.error or "send failed"
                self._record_send_failure(self._last_send_error)
                if index:
                    return SendResult(
                        success=False,
                        error=f"partial delivery: {self._last_send_error}",
                    )
                return result
            self._last_send_error = ""
            self._send_stats.sent_count += 1
            self._send_stats.last_success_at = _now()
            self._send_stats.last_error = ""
            last_result = result
        return last_result

    async def send_artifact(
        self,
        chat_id: str,
        *,
        kind: str,
        path: Path,
        filename: str,
        mime_type: str,
    ) -> SendResult:
        """Send one managed artifact. Concrete adapters opt in via capabilities."""
        return SendResult(success=False, error=f"{kind} delivery is not supported")

    @abstractmethod
    async def get_chat_info(self, chat_id: str) -> ChatInfo:
        """Return chat metadata for system prompt & logging."""

    # ── Gateway injection ─────────────────────────────

    def set_message_handler(self, handler: Callable[[MessageEvent], Awaitable[str | None]]) -> None:
        self._message_handler = handler

    def set_attachment_store(self, store: AttachmentStore) -> None:
        self._attachment_store = store

    async def prepare_inbound_attachments(self, event: MessageEvent) -> MessageEvent:
        """Resolve/cache inbound attachments after gateway authorization."""
        envelope = event.to_envelope()
        if not envelope.attachments:
            return event

        prepared: list[AttachmentRef] = []
        for ref in envelope.attachments:
            prepared.append(await self._prepare_attachment_ref(ref, event.source))
        envelope.attachments = prepared
        event.envelope = envelope
        return event

    async def download_attachment(
        self,
        ref: AttachmentRef,
        source: SessionSource | None = None,
    ) -> DownloadedAttachment:
        """Download a platform-private attachment id. Override in concrete adapters."""
        raise AttachmentDownloadError("platform_download_unavailable")

    # ── message pipeline (subclass should NOT override) ──

    def handle_message(self, event: MessageEvent) -> None:
        """Forward an inbound event immediately; Coordinator owns ordering."""
        self._last_message_at = _now()
        if self._is_duplicate_message(event):
            logger.info(
                "Platform %s dropped duplicate message id=%s",
                self._platform_name,
                event.message_id,
            )
            return

        asyncio.create_task(self._process_message(event))

    async def _process_message(self, event: MessageEvent) -> None:
        try:
            try:
                await self._send_typing(event.source.chat_id)
            except Exception:
                logger.exception("Typing indicator failed for coordinator submission")
            response = await self._message_handler(event) if self._message_handler else None
            self._last_response_at = _now()
            if response:
                logger.error(
                    "Platform handler returned outbound text outside DeliveryService: %s",
                    self._platform_name,
                )
        except Exception:
            logger.exception("Platform message forwarding failed")

    async def _prepare_attachment_ref(
        self,
        ref: AttachmentRef,
        source: SessionSource | None,
    ) -> AttachmentRef:
        skip_reason = _attachment_prepare_skip_reason(self.config, ref)
        if skip_reason:
            return _ref_with_resolution(ref, status="skipped", reason=skip_reason)

        store = self._get_attachment_store()
        if store is None:
            return _ref_with_resolution(ref, status="failed", reason="attachment_store_unavailable")

        try:
            if ref.local_path:
                resolved = store.store_local_path(ref.local_path, ref=ref)
            elif ref.url:
                if not bool(getattr(self.config, "attachments_download_urls", True)):
                    return _ref_with_resolution(
                        ref,
                        status="skipped",
                        reason="url_download_disabled",
                    )
                resolved = store.store_url(ref.url, ref=ref)
            elif ref.platform_file_id:
                if not bool(getattr(self.config, "attachments_download_platform_files", True)):
                    return _ref_with_resolution(
                        ref,
                        status="skipped",
                        reason="platform_download_disabled",
                    )
                downloaded = await self.download_attachment(ref, source=source)
                resolved = store.store_downloaded(downloaded, ref=ref)
            else:
                return _ref_with_resolution(
                    ref,
                    status="skipped",
                    reason="attachment_has_no_resolvable_location",
                )
        except AttachmentDownloadError as exc:
            return _ref_with_resolution(
                ref,
                status="failed",
                reason=exc.reason,
                error=str(exc),
            )
        except AttachmentStoreError as exc:
            return _ref_with_resolution(
                ref,
                status="failed",
                reason=exc.reason,
                error=str(exc),
            )
        except Exception as exc:
            return _ref_with_resolution(
                ref,
                status="failed",
                reason="resolve_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        return _ref_from_resolved(ref, resolved)

    def _get_attachment_store(self) -> AttachmentStore | None:
        if self._attachment_store is not None:
            return self._attachment_store
        data_dir = getattr(self.config, "agent_data_dir", None)
        if data_dir is None:
            return None
        self._attachment_store = AttachmentStore(Path(data_dir) / "attachments")
        return self._attachment_store

    # ── helpers ───────────────────────────────────────

    def _max_outbound_text_length(self) -> int:
        try:
            return int(getattr(self.capabilities, "max_text_length", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _message_dedupe_key(self, event: MessageEvent) -> str:
        message_id = str(event.message_id or "").strip()
        if not message_id:
            return ""
        source = event.source
        return ":".join([
            str(getattr(source, "platform", "")),
            str(getattr(source, "chat_id", "")),
            str(getattr(source, "user_id", "")),
            message_id,
        ])

    def _is_duplicate_message(self, event: MessageEvent) -> bool:
        key = self._message_dedupe_key(event)
        if not key:
            return False
        if key in self._seen_message_keys:
            self._seen_message_keys.move_to_end(key)
            return True
        self._seen_message_keys[key] = None
        while len(self._seen_message_keys) > self._dedupe_max_size:
            self._seen_message_keys.popitem(last=False)
        return False

    async def _send_typing(self, chat_id: str) -> None:
        """Optional typing indicator — override in subclass if platform supports it."""
        pass

    def _record_send_failure(self, error: str) -> None:
        self._send_stats.failed_count += 1
        self._send_stats.last_error = error
        self._send_stats.last_error_at = _now()

    def mark_connected(self, *, name: str | None = None) -> None:
        if name:
            self._platform_name = name
        self._connected = True
        self._last_connected_at = _now()
        self._last_connect_error = ""

    def mark_disconnected(self) -> None:
        self._connected = False
        self._last_disconnected_at = _now()

    def mark_connect_error(self, error: str, *, name: str | None = None) -> None:
        if name:
            self._platform_name = name
        self._connected = False
        self._last_connect_error = error
        self._last_disconnected_at = _now()

    def health_snapshot(self) -> dict:
        return {
            "name": self._platform_name,
            "adapter": type(self).__name__,
            "connected": self._connected,
            "last_connected_at": self._last_connected_at,
            "last_disconnected_at": self._last_disconnected_at,
            "last_connect_error": self._last_connect_error,
            "last_send_error": self._last_send_error,
            "dedupe_size": len(self._seen_message_keys),
            "dedupe_max_size": self._dedupe_max_size,
            "capabilities": self.capabilities.as_dict(),
            "last_message_at": self._last_message_at,
            "last_response_at": self._last_response_at,
            "send_stats": self._send_stats.snapshot(),
        }


def split_text_for_platform(text: str, limit: int) -> list[str]:
    """Split outbound text to fit platform message limits."""
    value = str(text or "")
    try:
        max_chars = int(limit)
    except (TypeError, ValueError):
        max_chars = 0
    if max_chars <= 0 or len(value) <= max_chars:
        return [value]

    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(current)
            current = ""

    for line in value.splitlines(keepends=True):
        remaining_line = line
        while remaining_line:
            room = max_chars - len(current)
            if room <= 0:
                flush()
                room = max_chars
            if len(remaining_line) <= room:
                current += remaining_line
                break
            current += remaining_line[:room]
            remaining_line = remaining_line[room:]
            flush()

    flush()
    return chunks or [value[:max_chars]]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _attachment_prepare_skip_reason(config, ref: AttachmentRef) -> str:
    if not bool(getattr(config, "multimodal_enabled", True)):
        return "multimodal_disabled"
    if _attachment_mode(config, ref) == "off":
        return "mode_off"
    if not bool(getattr(config, "attachments_resolve_inbound", True)):
        return "resolve_inbound_disabled"
    if not bool(getattr(config, "attachments_cache_inbound", True)):
        return "cache_disabled"
    return ""


def _attachment_mode(config, ref: AttachmentRef) -> str:
    kind = _canonical_attachment_kind(ref.kind)
    attr = {
        "image": "multimodal_image_mode",
        "audio": "multimodal_audio_mode",
        "video": "multimodal_video_mode",
        "file": "multimodal_file_mode",
    }.get(kind, "multimodal_file_mode")
    return str(getattr(config, attr, "auto") or "auto").strip().lower()


def _canonical_attachment_kind(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"image", "photo", "picture", "img"}:
        return "image"
    if normalized in {"voice", "record", "audio", "sound"}:
        return "audio"
    if normalized in {"video", "movie"}:
        return "video"
    if normalized in {"file", "document", "doc", "attachment"}:
        return "file"
    return normalized or "file"


def _ref_from_resolved(ref: AttachmentRef, resolved: ResolvedAttachment) -> AttachmentRef:
    return AttachmentRef(
        id=ref.id or resolved.id,
        kind=resolved.kind or ref.kind,
        name=resolved.name or ref.name,
        mime_type=resolved.mime_type or ref.mime_type,
        size=resolved.size or ref.size,
        url=ref.url or resolved.source_url,
        platform_file_id=ref.platform_file_id or resolved.platform_file_id,
        local_path=resolved.local_path,
        metadata=_resolution_metadata(
            ref,
            status="resolved",
            reason="cached",
            resolved=resolved,
        ),
    )


def _ref_with_resolution(
    ref: AttachmentRef,
    *,
    status: str,
    reason: str,
    error: str = "",
) -> AttachmentRef:
    return AttachmentRef(
        id=ref.id,
        kind=ref.kind,
        name=ref.name,
        mime_type=ref.mime_type,
        size=ref.size,
        url=ref.url,
        platform_file_id=ref.platform_file_id,
        local_path=ref.local_path,
        metadata=_resolution_metadata(ref, status=status, reason=reason, error=error),
    )


def _resolution_metadata(
    ref: AttachmentRef,
    *,
    status: str,
    reason: str,
    error: str = "",
    resolved: ResolvedAttachment | None = None,
) -> dict:
    metadata = dict(ref.metadata or {})
    if resolved is not None:
        metadata.update(dict(resolved.metadata or {}))
    payload = {
        "status": status,
        "reason": reason,
    }
    if error:
        payload["error"] = error
    if resolved is not None:
        payload.update({
            "id": resolved.id,
            "sha256": resolved.sha256,
            "local_path": resolved.local_path,
            "source_url": resolved.source_url,
            "size": resolved.size,
            "mime_type": resolved.mime_type,
        })
    metadata["attachment_resolve"] = payload
    return metadata
