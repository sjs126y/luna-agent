"""QQ adapter — generic HTTP bot bridge.

The adapter uses a small OneBot-compatible HTTP surface when available:
send_private_msg / send_group_msg for outbound messages, and a public
handle_webhook_payload method for future HTTP gateway integration.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import time
from pathlib import Path
from typing import Any

import aiohttp

from personal_agent.models.messages import MessageEnvelope, MessageEvent, MessagePart, SessionSource
from personal_agent.platforms.core import (
    BasePlatformAdapter,
    ChatInfo,
    PlatformCapabilities,
    SendResult,
)

logger = logging.getLogger(__name__)


class QQAdapter(BasePlatformAdapter):
    capabilities = PlatformCapabilities(
        text=True,
        rich_text=True,
        image_send=True,
        audio_send=True,
        video_send=True,
        mention=True,
        reply=True,
        attachments_in=True,
        max_text_length=4000,
    )
    MAX_MESSAGE_LENGTH = 4000

    def __init__(self, config, db) -> None:
        super().__init__(config, db)
        self._base_url: str = str(getattr(config, "qq_bot_base_url", "") or "").rstrip("/")
        self._token: str = str(getattr(config, "qq_bot_token", "") or "")
        self._webhook_secret: str = str(getattr(config, "qq_bot_webhook_secret", "") or "")
        self._session: aiohttp.ClientSession | None = None

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        if not self._base_url:
            error = "QQ bot base URL not configured"
            logger.warning(error)
            self.mark_connect_error(error, name="qq")
            raise RuntimeError(error)
        timeout = aiohttp.ClientTimeout(total=30)
        self._session = aiohttp.ClientSession(trust_env=True, timeout=timeout)
        await self.hooks.fire("on_connect")
        self.mark_connected(name="qq")
        logger.info("QQ adapter connected via HTTP base_url=%s", self._base_url)

    async def disconnect(self) -> None:
        await self.hooks.fire("on_disconnect")
        if self._session:
            await self._session.close()
            self._session = None
        self.mark_disconnected()
        logger.info("QQ adapter disconnected")

    async def send(self, chat_id: str, content: str) -> SendResult:
        if not self._session:
            return SendResult(success=False, error="Not connected")

        endpoint, payload = self._build_send_request(chat_id, content)
        try:
            result = await self._post_json(endpoint, payload)
            if _is_success_response(result):
                message_id = result.get("message_id") or (result.get("data") or {}).get("message_id")
                return SendResult(success=True, message_id=str(message_id) if message_id else None)
            return SendResult(success=False, error=_response_error(result))
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def get_chat_info(self, chat_id: str) -> ChatInfo:
        chat_type, raw_id = _split_chat_id(chat_id)
        if chat_type == "group":
            return ChatInfo(chat_id=chat_id, chat_type="group")
        return ChatInfo(chat_id=raw_id, chat_type="dm")

    async def handle_webhook_payload(self, payload: dict[str, Any], *, signature: str = "") -> bool:
        """Parse an inbound QQ/OneBot payload and enqueue it for the gateway."""
        if not self._verify_signature(payload, signature):
            logger.warning("QQ webhook signature rejected")
            return False

        modified = await self.hooks.fire("on_before_parse", payload)
        if modified is not None:
            payload = modified

        event = self._parse_payload(payload)
        if event is None:
            return False

        modified_event = await self.hooks.fire("on_after_parse", event, payload)
        if modified_event is not None:
            event = modified_event
        self.handle_message(event)
        return True

    def _build_send_request(self, chat_id: str, content: str) -> tuple[str, dict[str, Any]]:
        chat_type, raw_id = _split_chat_id(chat_id)
        message = _build_outbound_message(content, self.MAX_MESSAGE_LENGTH)
        if chat_type == "group":
            return "send_group_msg", {"group_id": raw_id, "message": message}
        return "send_private_msg", {"user_id": raw_id, "message": message}

    async def _post_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._session:
            raise RuntimeError("QQ HTTP session is not connected")
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        async with self._session.post(url, json=payload, headers=headers) as resp:
            data = await resp.json(content_type=None)
            if not resp.ok:
                raise RuntimeError(f"QQ HTTP {resp.status}: {str(data)[:200]}")
            return data if isinstance(data, dict) else {"data": data}

    def _parse_payload(self, payload: dict[str, Any]) -> MessageEvent | None:
        post_type = str(payload.get("post_type") or "")
        if post_type and post_type != "message":
            return None

        message_type = str(payload.get("message_type") or "private")
        text, parts, attachments = _extract_structured_message(
            payload.get("message") or payload.get("raw_message") or ""
        )
        if not text:
            return None

        user_id = str(payload.get("user_id") or payload.get("sender", {}).get("user_id") or "")
        user_name = str(
            payload.get("sender", {}).get("nickname")
            or payload.get("sender", {}).get("card")
            or ""
        )
        if message_type == "group":
            group_id = str(payload.get("group_id") or "")
            chat_id = f"group:{group_id}" if group_id else user_id
            chat_type = "group"
        else:
            chat_id = f"private:{user_id}" if user_id else ""
            chat_type = "dm"

        source = SessionSource(
            platform="qq",
            user_id=user_id,
            user_name=user_name,
            chat_id=chat_id,
            chat_type=chat_type,
        )
        message_id = str(payload.get("message_id") or "")
        event = MessageEvent(
            text=text,
            message_type="command" if text.startswith("/") else "text",
            source=source,
            parts=parts,
            attachments=attachments,
            raw_message=payload,
            message_id=message_id,
            timestamp=float(payload.get("time") or time.time()),
        )
        event.envelope = MessageEnvelope(
            id=message_id,
            source=source,
            text=text,
            parts=parts,
            attachments=[
                part.to_attachment_ref(f"{message_id or 'qq'}:{index}")
                for index, part in enumerate(attachments, start=1)
            ],
            thread_id=source.thread_id,
            raw=payload,
            metadata={"message_type": event.message_type},
        )
        return event

    def _verify_signature(self, payload: dict[str, Any], signature: str) -> bool:
        if not self._webhook_secret:
            return True
        if not signature:
            return False
        import json

        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hmac.new(
            self._webhook_secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha1,
        ).hexdigest()
        expected = f"sha1={digest}"
        return hmac.compare_digest(signature, expected) or hmac.compare_digest(signature, digest)


def _split_chat_id(chat_id: str) -> tuple[str, str]:
    value = str(chat_id or "")
    if ":" in value:
        prefix, raw = value.split(":", 1)
        if prefix in {"group", "private"}:
            return prefix, raw
    return "private", value


def _extract_text(message: Any) -> str:
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, list):
        parts = []
        for item in message:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                data = item.get("data") or {}
                parts.append(str(data.get("text") or ""))
        return "".join(parts).strip()
    return str(message or "").strip()


def _extract_message_summary(message: Any) -> str:
    return _extract_structured_message(message)[0]


def _extract_structured_message(message: Any) -> tuple[str, list[MessagePart], list[MessagePart]]:
    if isinstance(message, str):
        text = message.strip()
        return text, [MessagePart(type="text", text=text)] if text else [], []
    if not isinstance(message, list):
        text = str(message or "").strip()
        return text, [MessagePart(type="text", text=text)] if text else [], []

    parts = []
    structured: list[MessagePart] = []
    attachments: list[MessagePart] = []
    for item in message:
        if not isinstance(item, dict):
            continue
        segment_type = str(item.get("type") or "")
        data = item.get("data") or {}
        if segment_type == "text":
            text = str(data.get("text") or "")
            parts.append(text)
            if text:
                structured.append(MessagePart(type="text", text=text))
        elif segment_type == "image":
            part = _media_part("image", data, ("file", "url", "summary"))
            parts.append(part.render_text())
            structured.append(part)
            attachments.append(part)
        elif segment_type == "record":
            part = _media_part("voice", data, ("file", "url"))
            parts.append(part.render_text())
            structured.append(part)
            attachments.append(part)
        elif segment_type == "video":
            part = _media_part("video", data, ("file", "url"))
            parts.append(part.render_text())
            structured.append(part)
            attachments.append(part)
        elif segment_type == "file":
            part = _media_part("file", data, ("name", "file", "url"))
            parts.append(part.render_text())
            structured.append(part)
            attachments.append(part)
        elif segment_type == "at":
            qq = str(data.get("qq") or "")
            parts.append(f"[@{qq or 'unknown'}]")
            structured.append(MessagePart(type="mention", text=qq or "unknown", file_id=qq))
        elif segment_type == "reply":
            message_id = str(data.get("id") or "")
            parts.append(f"[reply:{message_id or 'unknown'}]")
            structured.append(MessagePart(type="quote", file_id=message_id or "unknown"))
        elif segment_type:
            parts.append(f"[{segment_type}]")
            structured.append(MessagePart(type=segment_type))
    return "".join(parts).strip(), structured, attachments


_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_RICH_MARKER_RE = re.compile(r"\[(image|record|voice|video|at|reply):([^\]]+)\]")


def _build_outbound_message(content: str, limit: int) -> str | list[dict[str, Any]]:
    text = str(content or "")
    segments: list[dict[str, Any]] = []
    cursor = 0
    for match in _iter_rich_matches(text):
        start, end, segment = match
        if start > cursor:
            _append_text_segment(segments, text[cursor:start])
        segments.append(segment)
        cursor = end
    if not segments:
        return _truncate_message(text, limit)
    if cursor < len(text):
        _append_text_segment(segments, text[cursor:])
    return segments


def _iter_rich_matches(text: str):
    matches = []
    for match in _MARKDOWN_IMAGE_RE.finditer(text):
        target = _normalize_media_target(match.group(2).strip())
        if target:
            matches.append((match.start(), match.end(), {"type": "image", "data": {"file": target}}))
    for match in _RICH_MARKER_RE.finditer(text):
        kind = match.group(1)
        value = match.group(2).strip()
        if not value:
            continue
        if kind == "image":
            segment = {"type": "image", "data": {"file": _normalize_media_target(value)}}
        elif kind in {"record", "voice"}:
            segment = {"type": "record", "data": {"file": _normalize_media_target(value)}}
        elif kind == "video":
            segment = {"type": "video", "data": {"file": _normalize_media_target(value)}}
        elif kind == "at":
            segment = {"type": "at", "data": {"qq": value}}
        else:
            segment = {"type": "reply", "data": {"id": value}}
        matches.append((match.start(), match.end(), segment))
    yield from sorted(matches, key=lambda item: item[0])


def _append_text_segment(segments: list[dict[str, Any]], text: str) -> None:
    if text:
        segments.append({"type": "text", "data": {"text": text}})


def _normalize_media_target(value: str) -> str:
    target = value.strip()
    if not target:
        return target
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", target):
        return target
    path = Path(target)
    if path.is_absolute():
        return path.as_uri()
    return target


def _media_placeholder(kind: str, data: dict[str, Any], keys: tuple[str, ...]) -> str:
    return _media_part(kind, data, keys).render_text()


def _media_part(kind: str, data: dict[str, Any], keys: tuple[str, ...]) -> MessagePart:
    detail = ""
    for key in keys:
        value = data.get(key)
        if value:
            detail = str(value)
            break
    return MessagePart(
        type=kind,
        text=detail,
        url=str(data.get("url") or ""),
        path=str(data.get("file") or ""),
        file_id=str(data.get("file_id") or data.get("id") or ""),
        name=str(data.get("name") or data.get("filename") or ""),
        metadata=dict(data),
    )


def _truncate_message(content: str, limit: int) -> str:
    text = str(content or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 32)] + f"\n...(truncated {len(text) - limit} chars)"


def _is_success_response(result: dict[str, Any]) -> bool:
    if "status" in result:
        return result.get("status") == "ok"
    if "retcode" in result:
        return result.get("retcode") == 0
    return result.get("error") is None


def _response_error(result: dict[str, Any]) -> str:
    message = result.get("message") or result.get("wording") or result.get("error")
    retcode = result.get("retcode")
    if message:
        return f"QQ API error retcode={retcode}: {message}"
    return f"QQ API error: {result}"
