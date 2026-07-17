"""QQ adapter backed by a NapCat-compatible OneBot 11 connection.

WebSocket carries inbound events and can also carry outbound actions. An
optional HTTP endpoint remains available for action calls when configured.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import mimetypes
import re
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

import aiohttp

from personal_agent.attachments import DownloadedAttachment
from personal_agent.attachments.store import DEFAULT_MAX_BYTES
from personal_agent.models.messages import MessageEnvelope, MessageEvent, MessagePart, SessionSource
from personal_agent.platforms.attachments import attachment_part, canonical_attachment_kind
from personal_agent.platforms.core import (
    AttachmentDownloadError,
    BasePlatformAdapter,
    ChatInfo,
    PlatformCapabilities,
    SendResult,
)
from personal_agent.tools.url_safety import check_url

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .companion import NapCatCompanion


class QQAdapter(BasePlatformAdapter):
    capabilities = PlatformCapabilities(
        text=True,
        rich_text=True,
        image_send=True,
        file_send=True,
        audio_send=True,
        video_send=True,
        mention=True,
        reply=True,
        attachments_in=True,
        max_text_length=4000,
        max_file_bytes=20 * 1024 * 1024,
        max_attachments=10,
    )
    MAX_MESSAGE_LENGTH = 4000

    def __init__(self, config, db, *, companion: NapCatCompanion | None = None) -> None:
        super().__init__(config, db)
        self._companion = companion
        self._base_url: str = str(getattr(config, "qq_bot_base_url", "") or "").rstrip("/")
        self._ws_url: str = str(getattr(config, "qq_bot_ws_url", "") or "").strip()
        self._token: str = str(getattr(config, "qq_bot_token", "") or "")
        self._webhook_secret: str = str(getattr(config, "qq_bot_webhook_secret", "") or "")
        self._session: aiohttp.ClientSession | None = None
        self._websocket: aiohttp.ClientWebSocketResponse | None = None
        self._ws_task: asyncio.Task | None = None
        self._ws_stop = asyncio.Event()
        self._ws_send_lock = asyncio.Lock()
        self._pending_actions: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._reconnect_delays = tuple(
            getattr(config, "platform_reconnect_delays", (1, 2, 5, 10, 30, 60))
        ) or (1, 2, 5, 10, 30, 60)
        self._ws_reconnect_attempts = 0
        self._last_ws_event_at = ""
        self._self_id = ""

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        if not self._ws_url:
            error = "QQ bot WebSocket URL not configured"
            logger.warning(error)
            self.mark_connect_error(error, name="qq")
            raise RuntimeError(error)
        timeout = aiohttp.ClientTimeout(total=30)
        self._session = aiohttp.ClientSession(trust_env=True, timeout=timeout)
        self._ws_stop = asyncio.Event()
        try:
            self._websocket = await self._connect_initial_websocket()
        except Exception:
            await self._session.close()
            self._session = None
            raise
        await self.hooks.fire("on_connect")
        self.mark_connected(name="qq")
        self._ws_task = asyncio.create_task(
            self._websocket_supervisor(self._websocket),
            name="qq-onebot-websocket",
        )
        logger.info(
            "QQ adapter connected via OneBot WebSocket ws_url=%s action_transport=%s",
            self._ws_url,
            "http" if self._base_url else "websocket",
        )

    async def disconnect(self) -> None:
        await self.hooks.fire("on_disconnect")
        self._ws_stop.set()
        self._fail_pending_actions("QQ adapter disconnected")
        if self._websocket is not None:
            await self._websocket.close()
            self._websocket = None
        if self._ws_task is not None:
            self._ws_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._ws_task
            self._ws_task = None
        if self._session:
            await self._session.close()
            self._session = None
        if self._companion is not None:
            await self._companion.stop()
        self.mark_disconnected()
        logger.info("QQ adapter disconnected")

    async def _connect_initial_websocket(self) -> aiohttp.ClientWebSocketResponse:
        companion = self._companion
        if companion is None or not companion.enabled:
            return await self._open_websocket()
        try:
            return await asyncio.wait_for(self._open_websocket(), timeout=3)
        except Exception as initial_error:
            started = await companion.ensure_started()
            logger.info(
                "Managed NapCat %s; waiting up to %.1fs for OneBot WebSocket",
                "started" if started else "already active",
                companion.startup_timeout_seconds,
            )
            try:
                return await self._wait_for_websocket(companion.startup_timeout_seconds)
            except Exception as exc:
                raise RuntimeError(
                    f"Managed NapCat did not expose QQ WebSocket {self._ws_url}: {exc}"
                ) from initial_error

    async def _wait_for_websocket(self, timeout_seconds: float) -> aiohttp.ClientWebSocketResponse:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last_error: Exception | None = None
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(last_error or "startup timeout")
            try:
                return await asyncio.wait_for(self._open_websocket(), timeout=min(3, remaining))
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(min(1, max(0, remaining)))

    async def _open_websocket(self) -> aiohttp.ClientWebSocketResponse:
        if self._session is None:
            raise RuntimeError("QQ HTTP session is not connected")
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            return await self._session.ws_connect(
                self._ws_url,
                headers=headers,
                heartbeat=30,
                autoping=True,
                max_msg_size=16 * 1024 * 1024,
                timeout=aiohttp.ClientWSTimeout(ws_close=10),
            )
        except Exception as exc:
            error = f"QQ WebSocket connect failed: {type(exc).__name__}: {exc}"
            self.mark_connect_error(error, name="qq")
            raise RuntimeError(error) from exc

    async def _websocket_supervisor(
        self,
        websocket: aiohttp.ClientWebSocketResponse,
    ) -> None:
        current = websocket
        while not self._ws_stop.is_set():
            try:
                await self._consume_websocket(current)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("QQ WebSocket receive loop failed")
            finally:
                if self._websocket is current:
                    self._websocket = None
                if not current.closed:
                    await current.close()
                self._fail_pending_actions("QQ WebSocket disconnected")

            if self._ws_stop.is_set():
                break
            self.mark_disconnected()
            current = await self._reconnect_websocket()
            if current is None:
                break

    async def _reconnect_websocket(self) -> aiohttp.ClientWebSocketResponse | None:
        attempt = 0
        while not self._ws_stop.is_set():
            delay = self._reconnect_delays[min(attempt, len(self._reconnect_delays) - 1)]
            attempt += 1
            self._ws_reconnect_attempts += 1
            logger.warning("QQ WebSocket disconnected; reconnecting in %ss", delay)
            try:
                await asyncio.wait_for(self._ws_stop.wait(), timeout=float(delay))
                return None
            except TimeoutError:
                pass
            if self._companion is not None and self._companion.enabled:
                try:
                    await self._companion.ensure_started()
                except Exception as exc:
                    logger.warning("Managed NapCat restart failed: %s", exc)
            try:
                websocket = await self._open_websocket()
            except Exception as exc:
                logger.warning("QQ WebSocket reconnect failed: %s", exc)
                continue
            self._websocket = websocket
            self.mark_connected(name="qq")
            logger.info("QQ WebSocket reconnected after %d attempt(s)", attempt)
            return websocket
        return None

    async def _consume_websocket(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        async for message in websocket:
            if message.type == aiohttp.WSMsgType.TEXT:
                await self._handle_websocket_text(str(message.data or ""))
            elif message.type == aiohttp.WSMsgType.BINARY:
                try:
                    await self._handle_websocket_text(bytes(message.data).decode("utf-8"))
                except UnicodeDecodeError:
                    logger.warning("QQ WebSocket ignored non-UTF-8 binary frame")
            elif message.type == aiohttp.WSMsgType.ERROR:
                error = websocket.exception()
                raise RuntimeError(f"QQ WebSocket error: {error}")
            elif message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
                break

    async def _handle_websocket_text(self, raw: str) -> None:
        import json

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("QQ WebSocket ignored invalid JSON frame: %s", raw[:200])
            return
        if not isinstance(payload, dict):
            logger.warning("QQ WebSocket ignored non-object frame")
            return
        self._last_ws_event_at = _timestamp()
        if payload.get("self_id") is not None:
            self._self_id = str(payload.get("self_id") or "")

        echo = str(payload.get("echo") or "")
        if echo:
            future = self._pending_actions.pop(echo, None)
            if future is not None and not future.done():
                future.set_result(payload)
            return
        await self._handle_onebot_payload(payload)

    async def _call_websocket_action(
        self,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        websocket = self._websocket
        if websocket is None or websocket.closed:
            raise RuntimeError("QQ WebSocket is not connected")
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending_actions[echo] = future
        try:
            async with self._ws_send_lock:
                await websocket.send_json({"action": action, "params": params, "echo": echo})
            return await asyncio.wait_for(future, timeout=30)
        finally:
            self._pending_actions.pop(echo, None)

    def _fail_pending_actions(self, reason: str) -> None:
        pending = list(self._pending_actions.values())
        self._pending_actions.clear()
        for future in pending:
            if not future.done():
                future.set_exception(RuntimeError(reason))

    def health_snapshot(self) -> dict[str, Any]:
        snapshot = super().health_snapshot()
        websocket = self._websocket
        snapshot.update({
            "ws_url": self._ws_url,
            "ws_connected": bool(websocket is not None and not websocket.closed),
            "action_transport": "http" if self._base_url else "websocket",
            "ws_reconnect_attempts": self._ws_reconnect_attempts,
            "pending_actions": len(self._pending_actions),
            "last_ws_event_at": self._last_ws_event_at,
            "self_id": self._self_id,
            "companion": (
                self._companion.snapshot()
                if self._companion is not None
                else {"mode": "external", "managed": False}
            ),
        })
        return snapshot

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

    async def send_artifact(
        self,
        chat_id: str,
        *,
        kind: str,
        path: Path,
        filename: str,
        mime_type: str,
    ) -> SendResult:
        if not self._session:
            return SendResult(success=False, error="Not connected")
        segment_type = {
            "image": "image",
            "audio": "record",
            "video": "video",
            "file": "file",
        }.get(kind)
        if segment_type is None:
            return SendResult(success=False, error=f"QQ does not support outbound {kind}")
        try:
            content = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            return SendResult(success=False, error=f"QQ artifact read failed: {exc}")
        if len(content) > self.capabilities.max_file_bytes:
            return SendResult(
                success=False,
                error=f"QQ artifact exceeds {self.capabilities.max_file_bytes} bytes",
            )
        segment_data = {
            "file": f"base64://{base64.b64encode(content).decode('ascii')}",
        }
        if kind == "file" and filename:
            segment_data["name"] = filename
        chat_type, raw_id = _split_chat_id(chat_id)
        payload = {
            "group_id" if chat_type == "group" else "user_id": raw_id,
            "message": [{
                "type": segment_type,
                "data": segment_data,
            }],
        }
        endpoint = "send_group_msg" if chat_type == "group" else "send_private_msg"
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

    async def download_attachment(
        self,
        ref,
        source: SessionSource | None = None,
    ) -> DownloadedAttachment:
        if not self._session:
            raise AttachmentDownloadError("platform_not_connected", "QQ HTTP session is not connected")
        data = _onebot_data(ref)
        file_id = str(ref.platform_file_id or data.get("file_id") or data.get("file") or "")
        if not file_id:
            raise AttachmentDownloadError("attachment_has_no_resolvable_location")

        errors: list[str] = []
        for endpoint, payload in _download_candidates(ref, file_id, data, source):
            try:
                result = await self._post_json(endpoint, payload)
            except Exception as exc:
                errors.append(f"{endpoint}: {type(exc).__name__}: {exc}")
                continue
            if not _is_success_response(result):
                errors.append(f"{endpoint}: {_response_error(result)}")
                continue
            payload_data = _result_data(result)
            target = _download_target(payload_data)
            inline = _inline_bytes(payload_data)
            try:
                if inline is not None:
                    content = inline
                    source_url = ""
                    mime_type = _download_mime(ref, data, payload_data, "")
                elif target:
                    content, source_url, mime_type = await self._read_download_target(
                        target,
                        kind=str(ref.kind or ""),
                        ref=ref,
                        data=data,
                        response_data=payload_data,
                    )
                else:
                    errors.append(f"{endpoint}: download target missing")
                    continue
            except AttachmentDownloadError as exc:
                errors.append(f"{endpoint}: {exc.reason}")
                continue
            return DownloadedAttachment(
                data=content,
                kind=canonical_attachment_kind(ref.kind),
                name=_download_name(ref, data, payload_data, target or file_id),
                mime_type=mime_type,
                source_url=source_url,
                platform_file_id=file_id,
                metadata={"onebot_download": payload_data},
            )

        detail = "; ".join(errors[-3:]) if errors else "no OneBot download candidate succeeded"
        raise AttachmentDownloadError("platform_download_unavailable", detail)

    async def handle_webhook_payload(self, payload: dict[str, Any], *, signature: str = "") -> bool:
        """Parse an inbound QQ/OneBot payload and enqueue it for the gateway."""
        if not self._verify_signature(payload, signature):
            logger.warning("QQ webhook signature rejected")
            return False

        return await self._handle_onebot_payload(payload)

    async def _handle_onebot_payload(self, payload: dict[str, Any]) -> bool:
        """Run an authenticated OneBot event through hooks and message parsing."""

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
        if not self._base_url:
            return await self._call_websocket_action(endpoint, payload)
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        async with self._session.post(url, json=payload, headers=headers) as resp:
            data = await resp.json(content_type=None)
            if not resp.ok:
                raise RuntimeError(f"QQ HTTP {resp.status}: {str(data)[:200]}")
            return data if isinstance(data, dict) else {"data": data}

    async def _read_download_target(
        self,
        target: str,
        *,
        kind: str,
        ref,
        data: dict[str, Any],
        response_data: dict[str, Any],
    ) -> tuple[bytes, str, str]:
        if _is_url(target):
            safety_error = check_url(target)
            if safety_error:
                raise AttachmentDownloadError("unsafe_url", safety_error)
            limit = _download_limit(kind)
            async with self._session.get(target) as resp:
                if not resp.ok:
                    raise AttachmentDownloadError("download_failed", f"QQ media HTTP {resp.status}")
                content = await resp.content.read(limit + 1)
                if len(content) > limit:
                    raise AttachmentDownloadError("size_exceeded")
                mime_type = _download_mime(ref, data, response_data, resp.headers.get("Content-Type", ""))
                return content, target, mime_type
        path = _target_path(target)
        if path is None or not path.exists() or not path.is_file():
            raise AttachmentDownloadError("download_target_unavailable")
        content = path.read_bytes()
        if len(content) > _download_limit(kind):
            raise AttachmentDownloadError("size_exceeded")
        return content, "", _download_mime(ref, data, response_data, mimetypes.guess_type(path.name)[0] or "")

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


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _onebot_data(ref) -> dict[str, Any]:
    metadata = dict(getattr(ref, "metadata", {}) or {})
    data = metadata.get("onebot_data")
    return dict(data) if isinstance(data, dict) else {}


def _download_candidates(
    ref,
    file_id: str,
    data: dict[str, Any],
    source: SessionSource | None,
) -> list[tuple[str, dict[str, Any]]]:
    kind = canonical_attachment_kind(getattr(ref, "kind", ""))
    candidates: list[tuple[str, dict[str, Any]]] = []
    if kind == "image":
        candidates.append(("get_image", {"file": file_id}))
    elif kind == "audio":
        payload = {"file": file_id}
        out_format = str(data.get("out_format") or data.get("format") or "")
        if out_format:
            payload["out_format"] = out_format
        candidates.append(("get_record", payload))
    if kind == "file":
        group_id = _source_group_id(source)
        busid = data.get("busid") or data.get("bus_id")
        if group_id and busid is not None:
            candidates.append(("get_group_file_url", {
                "group_id": group_id,
                "file_id": file_id,
                "busid": busid,
            }))
    candidates.extend([
        ("get_file", {"file_id": file_id}),
        ("get_file", {"file": file_id}),
    ])
    return _dedupe_candidates(candidates)


def _source_group_id(source: SessionSource | None) -> str:
    chat_id = str(getattr(source, "chat_id", "") or "")
    if chat_id.startswith("group:"):
        return chat_id.split(":", 1)[1]
    return ""


def _dedupe_candidates(candidates: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    result: list[tuple[str, dict[str, Any]]] = []
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for endpoint, payload in candidates:
        key = (endpoint, tuple(sorted((key, str(value)) for key, value in payload.items())))
        if key in seen:
            continue
        seen.add(key)
        result.append((endpoint, payload))
    return result


def _result_data(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data")
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        return {"file": data}
    return result


def _download_target(data: dict[str, Any]) -> str:
    for key in ("url", "download_url", "file_url", "file", "file_path", "path"):
        value = data.get(key)
        if value:
            return str(value)
    return ""


def _inline_bytes(data: dict[str, Any]) -> bytes | None:
    value = data.get("base64") or data.get("content_base64")
    if value:
        return base64.b64decode(str(value))
    content = data.get("content")
    if isinstance(content, bytes):
        return content
    return None


def _download_name(ref, source_data: dict[str, Any], response_data: dict[str, Any], fallback: str) -> str:
    value = (
        getattr(ref, "name", "")
        or source_data.get("name")
        or source_data.get("filename")
        or response_data.get("name")
        or response_data.get("filename")
        or Path(str(fallback or "attachment")).name
    )
    return str(value or "attachment")


def _download_mime(ref, source_data: dict[str, Any], response_data: dict[str, Any], declared: str) -> str:
    explicit = (
        getattr(ref, "mime_type", "")
        or source_data.get("mime_type")
        or source_data.get("mime")
        or response_data.get("mime_type")
        or response_data.get("mime")
    )
    if explicit:
        return str(explicit)
    declared = str(declared or "").split(";", 1)[0]
    if declared and declared != "application/octet-stream":
        return declared
    name = _download_name(ref, source_data, response_data, "")
    return mimetypes.guess_type(name)[0] or ""


def _is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"}


def _target_path(value: str) -> Path | None:
    parsed = urlparse(value)
    if parsed.scheme == "file":
        return Path(unquote(parsed.path)).expanduser()
    if parsed.scheme:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else None


def _download_limit(kind: str) -> int:
    normalized = canonical_attachment_kind(kind)
    return int(DEFAULT_MAX_BYTES.get(normalized, DEFAULT_MAX_BYTES["file"]))


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
            part = _media_part("audio", data, ("file", "url"))
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
    return attachment_part(
        kind=canonical_attachment_kind(kind),
        data=data,
        text=detail,
        name=str(data.get("name") or data.get("filename") or ""),
        mime_type=str(data.get("mime_type") or data.get("mime") or ""),
        metadata_key="onebot_data",
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
