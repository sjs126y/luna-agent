"""Platform core — base adapter, message pipeline, and registry."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Callable, Awaitable
from dataclasses import dataclass, field
from pathlib import Path

from personal_agent.attachments import AttachmentStore, DownloadedAttachment, ResolvedAttachment
from personal_agent.attachments.store import AttachmentStoreError
from personal_agent.models.messages import AttachmentRef, MessageEvent, OutboundMessage, PlatformCapabilities
from personal_agent.models.messages import SessionSource
from personal_agent.platforms.hooks import AdapterHooks

logger = logging.getLogger(__name__)

CHAT_LOCKS_MAXSIZE = 64
PENDING_WARNING_THRESHOLD = 10
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
class PendingMessage:
    event: MessageEvent
    queued_at: str
    queued_monotonic: float


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
    """Subclass implements connect/disconnect/send/get_chat_info.
    Base implements handle_message pipeline + retry + queue draining.
    """

    capabilities = PlatformCapabilities()

    def __init__(self, config, db) -> None:
        self.config = config
        self.db = db
        self._loop: asyncio.AbstractEventLoop | None = None
        self._message_handler: Callable[[MessageEvent], Awaitable[str | None]] | None = None
        self._message_bypass_predicate: Callable[[MessageEvent], bool] | None = None
        self._before_send_handler: Callable[[SessionSource, str], Awaitable[str | None]] | None = None
        self._after_send_handler: Callable[[SessionSource, str, bool, str], Awaitable[None]] | None = None
        self._active_sessions: dict[str, bool] = {}
        self._pending_messages: dict[str, list[PendingMessage]] = {}
        self._chat_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._seen_message_keys: OrderedDict[str, None] = OrderedDict()
        self._chat_locks_maxsize = _int_setting(
            getattr(config, "platform_chat_locks_maxsize", CHAT_LOCKS_MAXSIZE),
            CHAT_LOCKS_MAXSIZE,
            minimum=1,
        )
        self._pending_warning_threshold = _int_setting(
            getattr(config, "platform_pending_warning_threshold", PENDING_WARNING_THRESHOLD),
            PENDING_WARNING_THRESHOLD,
            minimum=1,
        )
        self._dedupe_max_size = _int_setting(
            getattr(config, "platform_message_dedupe_max_size", MESSAGE_DEDUPE_MAXSIZE),
            MESSAGE_DEDUPE_MAXSIZE,
            minimum=1,
        )
        self._send_max_retries = _int_setting(
            getattr(config, "platform_send_max_retries", 2),
            2,
            minimum=0,
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
        """Send a structured message, falling back to legacy text send."""
        return await self.send(chat_id, message.render_text())

    @abstractmethod
    async def get_chat_info(self, chat_id: str) -> ChatInfo:
        """Return chat metadata for system prompt & logging."""

    # ── Gateway injection ─────────────────────────────

    def set_message_handler(self, handler: Callable[[MessageEvent], Awaitable[str | None]]) -> None:
        self._message_handler = handler

    def set_message_bypass_predicate(self, predicate: Callable[[MessageEvent], bool] | None) -> None:
        self._message_bypass_predicate = predicate

    def set_outbound_handlers(
        self,
        *,
        before_send: Callable[[SessionSource, str], Awaitable[str | None]] | None = None,
        after_send: Callable[[SessionSource, str, bool, str], Awaitable[None]] | None = None,
    ) -> None:
        self._before_send_handler = before_send
        self._after_send_handler = after_send

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
        """Entry point. Returns in ~200us. Schedules background processing."""
        self._last_message_at = _now()
        if self._is_duplicate_message(event):
            logger.info(
                "Platform %s dropped duplicate message id=%s",
                self._platform_name,
                event.message_id,
            )
            return

        session_key = self._make_session_key(event.source)
        if self._should_bypass_queue(event):
            asyncio.create_task(self._process_bypass_message_background(event, session_key))
            return
        if session_key in self._active_sessions:
            queue = self._pending_messages.setdefault(session_key, [])
            queue.append(PendingMessage(event, queued_at=_now(), queued_monotonic=time.monotonic()))
            if len(queue) > self._pending_warning_threshold:
                logger.warning(
                    "Platform %s session %s has %d pending messages",
                    self._platform_name,
                    session_key,
                    len(queue),
                )
            return
        self._active_sessions[session_key] = True
        asyncio.create_task(self._process_message_background(event, session_key))

    async def _process_message_background(self, event: MessageEvent, session_key: str) -> None:
        """Background task: typing → Gateway → send → drain queue."""
        try:
            # per-chat serialization lock
            chat_id = event.source.chat_id or session_key
            lock = self._get_chat_lock(chat_id)
            async with lock:
                try:
                    await self._send_typing(event.source.chat_id)
                except Exception:
                    logger.exception("Typing indicator failed for session %s", session_key)
                response = None
                if self._message_handler:
                    response = await self._message_handler(event)
                    self._last_response_at = _now()
                if response:
                    await self._send_gateway_response(event.source, response)
        except Exception:
            logger.exception("Background processing failed for session %s", session_key)
        finally:
            self._active_sessions.pop(session_key, None)
            # Drain pending one-at-a-time via NEW task (not recursion — C stack safety)
            queue = self._pending_messages.get(session_key)
            if queue:
                next_message = queue.pop(0)
                if not queue:
                    del self._pending_messages[session_key]
                self._active_sessions[session_key] = True
                asyncio.create_task(self._process_message_background(next_message.event, session_key))

    async def _process_bypass_message_background(self, event: MessageEvent, session_key: str) -> None:
        """Process a control reply while the normal session turn is waiting."""
        try:
            response = None
            if self._message_handler:
                response = await self._message_handler(event)
                self._last_response_at = _now()
            if response:
                await self._send_gateway_response(event.source, response)
        except Exception:
            logger.exception("Bypass processing failed for session %s", session_key)

    def _should_bypass_queue(self, event: MessageEvent) -> bool:
        if self._message_bypass_predicate is None:
            return False
        try:
            return bool(self._message_bypass_predicate(event))
        except Exception:
            logger.exception("Message bypass predicate failed")
            return False

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

    # ── retry logic ───────────────────────────────────

    async def _send_gateway_response(self, source: SessionSource, content: str) -> bool:
        final = str(content or "")
        if self._before_send_handler is not None:
            try:
                transformed = await self._before_send_handler(source, final)
            except Exception:
                logger.exception("Gateway before-send handler failed")
            else:
                if transformed is None:
                    return False
                final = str(transformed)
        if not final:
            return False
        success = await self._send_with_retry(source.chat_id, final)
        if self._after_send_handler is not None:
            try:
                await self._after_send_handler(source, final, success, self._last_send_error)
            except Exception:
                logger.exception("Gateway after-send handler failed")
        return success

    async def _send_with_retry(
        self,
        chat_id: str,
        content: str,
        max_retries: int | None = None,
    ) -> bool:
        max_retries = self._send_max_retries if max_retries is None else max_retries
        for chunk in split_text_for_platform(content, self._max_outbound_text_length()):
            sent = await self._send_chunk_with_retry(chat_id, chunk, max_retries=max_retries)
            if not sent:
                return False
        return True

    async def _send_chunk_with_retry(self, chat_id: str, content: str, *, max_retries: int) -> bool:
        message = OutboundMessage.text(content)
        for attempt in range(max_retries + 1):
            try:
                self._send_stats.last_send_at = _now()
                result = await self.send_message(chat_id, message)
                if result.success:
                    self._last_send_error = ""
                    self._send_stats.sent_count += 1
                    self._send_stats.last_success_at = _now()
                    self._send_stats.last_error = ""
                    return True

                error_lower = (result.error or "").lower()
                self._last_send_error = result.error or "send failed"

                # Timeout → never retry (message may have been delivered)
                if "timeout" in error_lower or "timed out" in error_lower:
                    self._record_send_failure(result.error or "timeout")
                    logger.error("Send timed out (attempt %d): %s", attempt + 1, result.error)
                    return False

                if attempt < max_retries:
                    self._send_stats.retry_count += 1
                    # Format error → strip Markdown, retry with plain text
                    if self._is_format_error(error_lower):
                        content = _strip_formatting(content)
                        message = OutboundMessage.text(content)
                    delay = self._send_retry_delay(attempt, error_lower)
                    logger.warning("Send failed (attempt %d/%d): %s, retrying in %.1fs",
                                   attempt + 1, max_retries, result.error, delay)
                    await self._sleep_before_retry(delay)
                else:
                    self._record_send_failure(result.error or "send failed")
                    logger.error("Send failed after %d retries: %s", max_retries, result.error)
                    return False

            except asyncio.TimeoutError:
                self._last_send_error = "timeout"
                self._record_send_failure("timeout")
                logger.error("Send timeout (attempt %d) — not retrying", attempt + 1)
                return False
            except Exception as exc:
                error_msg = str(exc).lower()
                self._last_send_error = f"{type(exc).__name__}: {exc}"
                # Timeout in exception → don't retry
                if "timeout" in error_msg or "timed out" in error_msg:
                    self._record_send_failure(self._last_send_error)
                    logger.error("Send timeout exception (attempt %d)", attempt + 1)
                    return False
                if attempt < max_retries:
                    self._send_stats.retry_count += 1
                    logger.warning("Send exception (attempt %d/%d): %s", attempt + 1, max_retries, exc)
                    await self._sleep_before_retry(self._send_retry_delay(attempt, error_msg))
                else:
                    self._record_send_failure(self._last_send_error)
                    return False
        return False

    # ── helpers ───────────────────────────────────────

    def _make_session_key(self, source) -> str:
        return f"{source.platform}:{source.chat_id}:{source.user_id}"

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

    def _get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        if chat_id not in self._chat_locks:
            if len(self._chat_locks) >= self._chat_locks_maxsize:
                self._chat_locks.popitem(last=False)
            self._chat_locks[chat_id] = asyncio.Lock()
        return self._chat_locks[chat_id]

    async def _send_typing(self, chat_id: str) -> None:
        """Optional typing indicator — override in subclass if platform supports it."""
        pass

    def _send_retry_delay(self, attempt: int, error_lower: str) -> float:
        if self._is_format_error(error_lower):
            return 0.5
        return (2 ** attempt) + random.uniform(0, 1)

    async def _sleep_before_retry(self, delay: float) -> None:
        await asyncio.sleep(delay)

    @staticmethod
    def _is_format_error(error_lower: str) -> bool:
        return "parse" in error_lower or "format" in error_lower or "markdown" in error_lower

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
        now = time.monotonic()
        pending_by_session = {
            key: len(items)
            for key, items in sorted(self._pending_messages.items())
        }
        oldest_pending_age = max(
            (now - item.queued_monotonic for items in self._pending_messages.values() for item in items),
            default=0.0,
        )
        return {
            "name": self._platform_name,
            "adapter": type(self).__name__,
            "connected": self._connected,
            "last_connected_at": self._last_connected_at,
            "last_disconnected_at": self._last_disconnected_at,
            "last_connect_error": self._last_connect_error,
            "last_send_error": self._last_send_error,
            "active_sessions": len(self._active_sessions),
            "active_session_keys": sorted(self._active_sessions),
            "pending_messages": sum(len(items) for items in self._pending_messages.values()),
            "pending_session_count": len(self._pending_messages),
            "pending_by_session": pending_by_session,
            "oldest_pending_age_seconds": round(oldest_pending_age, 3),
            "chat_locks": len(self._chat_locks),
            "chat_locks_maxsize": self._chat_locks_maxsize,
            "dedupe_size": len(self._seen_message_keys),
            "dedupe_max_size": self._dedupe_max_size,
            "pending_warning_threshold": self._pending_warning_threshold,
            "send_max_retries": self._send_max_retries,
            "capabilities": self.capabilities.as_dict(),
            "last_message_at": self._last_message_at,
            "last_response_at": self._last_response_at,
            "send_stats": self._send_stats.snapshot(),
        }


def _strip_formatting(text: str) -> str:
    """Remove common Markdown formatting characters."""
    import re
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'\[(.+?)\]\(.+?\)', r'\1', text)
    return text


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
