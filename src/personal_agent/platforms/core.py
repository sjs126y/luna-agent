"""Platform core — base adapter, message pipeline, and registry."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Callable, Awaitable
from dataclasses import dataclass

from personal_agent.agent.hooks import Hooks
from personal_agent.models.messages import MessageEvent

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


# ── PlatformEntry & PlatformRegistry ─────────────────

@dataclass
class PlatformEntry:
    name: str
    factory: Callable[..., "BasePlatformAdapter"]
    check_fn: Callable[[object], bool]  # config → bool


class PlatformRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, PlatformEntry] = {}

    def register(self, entry: PlatformEntry) -> None:
        self._entries[entry.name] = entry

    def unregister(self, name: str) -> None:
        self._entries.pop(name, None)

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

    def __init__(self, config, db) -> None:
        self.config = config
        self.db = db
        self._loop: asyncio.AbstractEventLoop | None = None
        self._message_handler: Callable[[MessageEvent], Awaitable[str | None]] | None = None
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
        self.hooks = Hooks()
        self._platform_name = type(self).__name__
        self._connected = False
        self._last_connected_at = ""
        self._last_disconnected_at = ""
        self._last_connect_error = ""
        self._last_send_error = ""
        self._last_message_at = ""
        self._last_response_at = ""
        self._send_stats = SendStats()

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

    @abstractmethod
    async def get_chat_info(self, chat_id: str) -> ChatInfo:
        """Return chat metadata for system prompt & logging."""

    # ── Gateway injection ─────────────────────────────

    def set_message_handler(self, handler: Callable[[MessageEvent], Awaitable[str | None]]) -> None:
        self._message_handler = handler

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
                    await self._send_with_retry(event.source.chat_id, response)
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

    # ── retry logic ───────────────────────────────────

    async def _send_with_retry(self, chat_id: str, content: str, max_retries: int | None = None) -> None:
        max_retries = self._send_max_retries if max_retries is None else max_retries
        for attempt in range(max_retries + 1):
            try:
                self._send_stats.last_send_at = _now()
                result = await self.send(chat_id, content)
                if result.success:
                    self._last_send_error = ""
                    self._send_stats.sent_count += 1
                    self._send_stats.last_success_at = _now()
                    self._send_stats.last_error = ""
                    return

                error_lower = (result.error or "").lower()
                self._last_send_error = result.error or "send failed"

                # Timeout → never retry (message may have been delivered)
                if "timeout" in error_lower or "timed out" in error_lower:
                    self._record_send_failure(result.error or "timeout")
                    logger.error("Send timed out (attempt %d): %s", attempt + 1, result.error)
                    return

                if attempt < max_retries:
                    self._send_stats.retry_count += 1
                    # Format error → strip Markdown, retry with plain text
                    if self._is_format_error(error_lower):
                        content = _strip_formatting(content)
                    delay = self._send_retry_delay(attempt, error_lower)
                    logger.warning("Send failed (attempt %d/%d): %s, retrying in %.1fs",
                                   attempt + 1, max_retries, result.error, delay)
                    await self._sleep_before_retry(delay)
                else:
                    self._record_send_failure(result.error or "send failed")
                    logger.error("Send failed after %d retries: %s", max_retries, result.error)

            except asyncio.TimeoutError:
                self._last_send_error = "timeout"
                self._record_send_failure("timeout")
                logger.error("Send timeout (attempt %d) — not retrying", attempt + 1)
                return
            except Exception as exc:
                error_msg = str(exc).lower()
                self._last_send_error = f"{type(exc).__name__}: {exc}"
                # Timeout in exception → don't retry
                if "timeout" in error_msg or "timed out" in error_msg:
                    self._record_send_failure(self._last_send_error)
                    logger.error("Send timeout exception (attempt %d)", attempt + 1)
                    return
                if attempt < max_retries:
                    self._send_stats.retry_count += 1
                    logger.warning("Send exception (attempt %d/%d): %s", attempt + 1, max_retries, exc)
                    await self._sleep_before_retry(self._send_retry_delay(attempt, error_msg))
                else:
                    self._record_send_failure(self._last_send_error)

    # ── helpers ───────────────────────────────────────

    def _make_session_key(self, source) -> str:
        return f"{source.platform}:{source.chat_id}:{source.user_id}"

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


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
