"""WeChat adapter — personal WeChat via Tencent iLink Bot API.

QR login → long-poll getupdates → sendmessage.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import secrets
import struct
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from personal_agent.attachments import DownloadedAttachment
from personal_agent.attachments.store import DEFAULT_MAX_BYTES
from personal_agent.platforms.core import (
    AttachmentDownloadError,
    BasePlatformAdapter,
    ChatInfo,
    PlatformCapabilities,
    SendResult,
    split_text_for_platform,
)
from personal_agent.platforms.attachments import attachment_part
from personal_agent.models.messages import AttachmentRef, MessageEnvelope, MessageEvent, MessagePart, SessionSource
from personal_agent.tools.url_safety import check_url

logger = logging.getLogger(__name__)

API_BASE = "https://ilinkai.weixin.qq.com"
CDN_API_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
DEDUP_MAXSIZE = 1000
DEDUP_TTL = 300


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode()).decode()


def _headers(token: str | None, body: str) -> dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


class WeChatAdapter(BasePlatformAdapter):
    supports_code_blocks = True
    capabilities = PlatformCapabilities(
        text=True,
        image_send=True,
        file_send=True,
        video_send=True,
        typing=True,
        attachments_in=True,
        max_text_length=2000,
        max_file_bytes=20 * 1024 * 1024,
        max_attachments=10,
    )
    MAX_MESSAGE_LENGTH = 2000

    def __init__(self, config, db) -> None:
        super().__init__(config, db)
        self._token: str = getattr(config, "weixin_token", "") or ""
        self._account_id: str = getattr(config, "weixin_account_id", "") or ""
        self._user_id: str = getattr(config, "weixin_user_id", "") or ""
        self._base_url: str = getattr(config, "weixin_base_url", "") or API_BASE
        self._cdn_base_url: str = getattr(config, "weixin_cdn_base_url", "") or CDN_API_BASE

        self._state_dir = config.agent_data_dir / "wechat"
        self._state_dir.mkdir(parents=True, exist_ok=True)

        self._poll_session: aiohttp.ClientSession | None = None
        self._send_session: aiohttp.ClientSession | None = None
        self._poll_task: asyncio.Task | None = None
        self._sync_buf = ""
        self._seen_ids: OrderedDict[str, float] = OrderedDict()
        self._context_tokens: dict[str, str] = {}

    # ── connect / disconnect ──────────────────────────

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._load_creds()

        if not self._token or not self._account_id:
            error = "WeChat not logged in. Run: uv run personal-agent wechat-login"
            logger.warning(error)
            self.mark_connect_error(error, name="wechat")
            raise RuntimeError(error)

        t = aiohttp.ClientTimeout(total=None, connect=None, sock_connect=None, sock_read=None)
        self._poll_session = aiohttp.ClientSession(trust_env=True, timeout=t)
        self._send_session = aiohttp.ClientSession(trust_env=True, timeout=t)
        self._load_sync_buf()
        self._poll_task = asyncio.create_task(self._poll_loop())
        await self.hooks.fire("on_connect")
        self.mark_connected(name="wechat")
        logger.info("✅ WeChat connected — account=%s..., polling started", self._account_id[:12])

    async def disconnect(self) -> None:
        await self.hooks.fire("on_disconnect")
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        if self._poll_session:
            await self._poll_session.close()
            self._poll_session = None
        if self._send_session:
            await self._send_session.close()
            self._send_session = None
        self.mark_disconnected()
        logger.info("WeChat adapter disconnected")

    # ── send ──────────────────────────────────────────

    async def send(self, chat_id: str, content: str) -> SendResult:
        if not self._send_session:
            return SendResult(success=False, error="Not connected")
        last_message_id = None
        for chunk in self._split_text(content):
            result = await self._send_one(chat_id, chunk)
            if not result.success:
                return result
            last_message_id = result.message_id
        return SendResult(success=True, message_id=last_message_id)

    async def _send_one(self, chat_id: str, content: str) -> SendResult:
        try:
            client_id = f"pa-weixin-{uuid.uuid4().hex[:12]}"
            payload = {
                "base_info": {"channel_version": CHANNEL_VERSION},
                "msg": {
                    "from_user_id": "",
                    "to_user_id": chat_id,
                    "client_id": client_id,
                    "message_type": 2,   # MSG_TYPE_BOT
                    "message_state": 2,   # MSG_STATE_FINISH
                    "item_list": [{"type": 1, "text_item": {"text": content}}],
                },
            }
            context_token = self._context_tokens.get(chat_id)
            if context_token:
                payload["msg"]["context_token"] = context_token
            result = await self._api("ilink/bot/sendmessage", payload, self._send_session, API_TIMEOUT_MS)
            logger.info("WeChat send result: ret=%s errcode=%s errmsg=%s",
                        result.get("ret"), result.get("errcode"), result.get("errmsg", ""))
            if result.get("errcode") in (0, None) and result.get("ret") in (0, None):
                return SendResult(success=True, message_id=client_id)
            return SendResult(success=False,
                error=f"WeChat error ret={result.get('ret')} errcode={result.get('errcode')}")
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
        if not self._send_session:
            return SendResult(success=False, error="Not connected")
        media_type = {"image": 1, "video": 2, "file": 3}.get(kind)
        if media_type is None:
            return SendResult(success=False, error=f"WeChat does not support outbound {kind}")
        try:
            uploaded = await self._upload_artifact(chat_id, path, media_type=media_type)
            media = {
                "encrypt_query_param": uploaded["download_param"],
                "aes_key": base64.b64encode(uploaded["aes_key"]).decode(),
                "encrypt_type": 1,
            }
            if kind == "image":
                item = {"type": 2, "image_item": {"media": media, "mid_size": uploaded["cipher_size"]}}
            elif kind == "video":
                item = {"type": 4, "video_item": {"media": media, "video_size": uploaded["cipher_size"]}}
            else:
                item = {
                    "type": 5,
                    "file_item": {
                        "media": media,
                        "file_name": filename,
                        "len": str(uploaded["raw_size"]),
                    },
                }
            return await self._send_item(chat_id, item)
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def _upload_artifact(self, chat_id: str, path: Path, *, media_type: int) -> dict:
        content = await asyncio.to_thread(path.read_bytes)
        aes_key = secrets.token_bytes(16)
        encrypted = AES.new(aes_key, AES.MODE_ECB).encrypt(pad(content, AES.block_size))
        file_key = secrets.token_hex(16)
        request = {
            "filekey": file_key,
            "media_type": media_type,
            "to_user_id": chat_id,
            "rawsize": len(content),
            "rawfilemd5": hashlib.md5(content).hexdigest(),  # noqa: S324 - protocol field
            "filesize": len(encrypted),
            "no_need_thumb": True,
            "aeskey": aes_key.hex(),
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        response = await self._api(
            "ilink/bot/getuploadurl",
            request,
            self._send_session,
            API_TIMEOUT_MS,
        )
        upload_url = str(response.get("upload_full_url") or "").strip()
        if not upload_url:
            upload_param = str(response.get("upload_param") or "")
            if not upload_param:
                raise RuntimeError("WeChat getuploadurl returned no upload URL")
            upload_url = (
                f"{self._cdn_base_url.rstrip('/')}/upload"
                f"?encrypted_query_param={quote(upload_param, safe='')}&filekey={quote(file_key, safe='')}"
            )
        async with self._send_session.post(
            upload_url,
            data=encrypted,
            headers={"Content-Type": "application/octet-stream"},
            timeout=aiohttp.ClientTimeout(total=API_TIMEOUT_MS / 1000),
        ) as uploaded:
            if uploaded.status != 200:
                detail = await uploaded.text()
                raise RuntimeError(f"WeChat CDN upload HTTP {uploaded.status}: {detail[:200]}")
            download_param = str(uploaded.headers.get("x-encrypted-param") or "")
            if not download_param:
                raise RuntimeError("WeChat CDN upload returned no x-encrypted-param")
        return {
            "download_param": download_param,
            "aes_key": aes_key,
            "raw_size": len(content),
            "cipher_size": len(encrypted),
        }

    async def _send_item(self, chat_id: str, item: dict) -> SendResult:
        client_id = f"pa-weixin-{uuid.uuid4().hex[:12]}"
        payload = {
            "base_info": {"channel_version": CHANNEL_VERSION},
            "msg": {
                "from_user_id": "",
                "to_user_id": chat_id,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "item_list": [item],
            },
        }
        context_token = self._context_tokens.get(chat_id)
        if context_token:
            payload["msg"]["context_token"] = context_token
        result = await self._api("ilink/bot/sendmessage", payload, self._send_session, API_TIMEOUT_MS)
        if result.get("errcode") in (0, None) and result.get("ret") in (0, None):
            return SendResult(success=True, message_id=client_id)
        return SendResult(
            success=False,
            error=f"WeChat error ret={result.get('ret')} errcode={result.get('errcode')}",
        )

    async def get_chat_info(self, chat_id: str) -> ChatInfo:
        return ChatInfo(chat_id=chat_id, chat_type="dm")

    async def _prepare_attachment_ref(
        self,
        ref: AttachmentRef,
        source: SessionSource | None,
    ) -> AttachmentRef:
        if _should_use_wechat_downloader(ref):
            ref = AttachmentRef(
                id=ref.id,
                kind=ref.kind,
                name=ref.name,
                mime_type=ref.mime_type,
                size=ref.size,
                url="",
                platform_file_id=ref.platform_file_id or _wechat_platform_file_id(ref),
                local_path=ref.local_path,
                metadata=dict(ref.metadata),
            )
        return await super()._prepare_attachment_ref(ref, source)

    async def download_attachment(
        self,
        ref: AttachmentRef,
        source: SessionSource | None = None,
    ) -> DownloadedAttachment:
        data = _wechat_data(ref)
        url = _wechat_download_url(data)
        if not url:
            raise AttachmentDownloadError("platform_download_unavailable", "wechat_download_url_unavailable")
        encrypted = _wechat_is_encrypted(data)
        key = b""
        if encrypted:
            key = _wechat_aes_key(data)
            if not key:
                raise AttachmentDownloadError("decrypt_key_unavailable")
        content, mime_type = await self._download_url_bytes(url, kind=ref.kind)
        if encrypted:
            content = _decrypt_wechat_payload(content, key)
        return DownloadedAttachment(
            data=content,
            kind=_media_kind_from_ref(ref),
            name=_wechat_download_name(ref, data),
            mime_type=ref.mime_type or data.get("mime_type") or data.get("mime") or mime_type,
            source_url=url,
            platform_file_id=ref.platform_file_id or _wechat_platform_file_id(ref),
            metadata={
                "wechat_download": {
                    "encrypted": encrypted,
                    "source_url": url,
                    "file_id": ref.platform_file_id or _wechat_platform_file_id(ref),
                }
            },
        )

    async def _download_url_bytes(self, url: str, *, kind: str) -> tuple[bytes, str]:
        safety_error = check_url(url)
        if safety_error:
            raise AttachmentDownloadError("unsafe_url", safety_error)
        limit = int(DEFAULT_MAX_BYTES.get(_media_kind_from_value(kind), DEFAULT_MAX_BYTES["file"]))
        close_session = False
        session = self._send_session or self._poll_session
        if session is None:
            timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_MS / 1000)
            session = aiohttp.ClientSession(trust_env=True, timeout=timeout)
            close_session = True
        try:
            async with session.get(url) as resp:
                if not resp.ok:
                    raise AttachmentDownloadError("download_failed", f"WeChat media HTTP {resp.status}")
                content = await _read_limited_response(resp, limit)
                mime_type = str(resp.headers.get("Content-Type") or "").split(";", 1)[0]
                return content, mime_type
        finally:
            if close_session:
                await session.close()

    # ── typing indicator ──────────────────────────────

    async def _send_typing(self, chat_id: str) -> None:
        """Start typing indicator (best-effort)."""
        if not self._send_session:
            return
        try:
            await self._api("ilink/bot/sendtyping", {
                "base_info": {"channel_version": CHANNEL_VERSION},
                "ilink_user_id": chat_id,
                "typing_ticket": "",
                "status": "start",
            }, self._send_session, API_TIMEOUT_MS)
        except Exception:
            pass

    # ── text chunking ─────────────────────────────────

    def _split_text(self, text: str) -> list[str]:
        """Split long text so every chunk fits the WeChat send limit."""
        return split_text_for_platform(text, self.MAX_MESSAGE_LENGTH)

    async def send_chunked(self, chat_id: str, content: str) -> None:
        """Send content, splitting if needed."""
        await self.send(chat_id, content)

    # ── long-poll loop ────────────────────────────────

    async def _poll_loop(self) -> None:
        timeout_ms = POLL_TIMEOUT_MS
        failures = 0
        while True:
            try:
                result = await self._api("ilink/bot/getupdates",
                    {"base_info": {"channel_version": CHANNEL_VERSION},
                     "get_updates_buf": self._sync_buf},
                    self._poll_session, timeout_ms)
                t = result.get("longpolling_timeout_ms")
                if isinstance(t, int) and t > 0:
                    timeout_ms = t
                if result.get("ret") in (0, None) and result.get("errcode") in (0, None):
                    failures = 0
                    self._sync_buf = result.get("get_updates_buf", self._sync_buf)
                    self._save_sync_buf()
                    msgs = result.get("msgs") or []
                    if msgs:
                        logger.info("WeChat poll: %d message(s) received", len(msgs))
                    for msg in msgs:
                        asyncio.create_task(self._process_update(msg))
                else:
                    failures += 1
                    logger.warning("WeChat poll error: ret=%s errcode=%s errmsg=%s",
                                   result.get("ret"), result.get("errcode"), result.get("errmsg", ""))
                    errcode = result.get("errcode")
                    if errcode == -14:
                        logger.warning("WeChat session expired, pausing 10min")
                        await asyncio.sleep(600)
                        failures = 0
                    elif failures > 3:
                        await asyncio.sleep(30)
                    else:
                        await asyncio.sleep(2)
            except asyncio.CancelledError:
                break
            except asyncio.TimeoutError:
                pass
            except Exception:
                failures += 1
                logger.exception("WeChat poll error (%d)", failures)
                await asyncio.sleep(min(2 ** failures, 30))

    # ── message processing ────────────────────────────

    async def _process_update(self, msg: dict) -> None:
        try:
            sender = str(msg.get("from_user_id") or "")
            if sender == self._account_id:
                return

            msg_id = str(msg.get("message_id") or "").strip()
            if msg_id and self._is_duplicate(msg_id):
                return

            text = ""
            media = []
            attachments: list[MessagePart] = []
            for item in (msg.get("item_list") or []):
                itype = item.get("type") or item.get("msg_type")
                if itype == 1 or itype == "text":  # ITEM_TEXT
                    ti = item.get("text_item") or {}
                    text += ti.get("text", "") or item.get("content", "")
                else:
                    part = _media_part(item)
                    attachments.append(part)
                    media.append(part.render_text())

            combined = text.strip()
            if not combined:
                combined = " ".join(media)
            elif media:
                combined = " ".join([combined, *media])
            if not combined:
                return

            context_token = _message_context_token(msg)
            if context_token:
                self._context_tokens[sender] = context_token

            source = SessionSource(
                platform="wechat",
                user_id=sender,
                user_name=msg.get("from_user_name", ""),
                chat_id=sender,
                chat_type="dm",
            )
            event = MessageEvent(
                text=combined,
                message_type="command" if combined.startswith("/") else "text",
                source=source,
                parts=([MessagePart(type="text", text=text.strip())] if text.strip() else []) + attachments,
                attachments=attachments,
                raw_message=msg,
                message_id=msg_id,
                timestamp=msg.get("create_time", time.time()),
            )
            event.envelope = MessageEnvelope(
                id=msg_id,
                source=source,
                text=combined,
                parts=event.parts,
                attachments=[
                    part.to_attachment_ref(f"{msg_id or 'wechat'}:{index}")
                    for index, part in enumerate(attachments, start=1)
                ],
                thread_id=source.thread_id,
                raw=msg,
                metadata={"message_type": event.message_type},
            )
            logger.info("WeChat inbound: user=%s text=%s",
                       sender[:12] if sender else "?", combined[:60])
            self.handle_message(event)
        except Exception:
            logger.exception("WeChat message processing failed")

    # ── dedup ─────────────────────────────────────────

    def _is_duplicate(self, msg_id: str) -> bool:
        now = time.time()
        if msg_id in self._seen_ids:
            return True
        self._seen_ids[msg_id] = now
        while len(self._seen_ids) > DEDUP_MAXSIZE:
            self._seen_ids.popitem(last=False)
        stale = [k for k, v in self._seen_ids.items() if now - v > DEDUP_TTL]
        for k in stale:
            self._seen_ids.pop(k, None)
        return False

    # ── API helper ────────────────────────────────────

    async def _api(self, path: str, payload: dict,
                   session: aiohttp.ClientSession, timeout_ms: int) -> dict:
        body = json.dumps(payload, ensure_ascii=False)
        url = f"{self._base_url.rstrip('/')}/{path}"
        headers = _headers(self._token, body)

        async def _do():
            async with session.post(url, data=body, headers=headers) as resp:
                raw = await resp.text()
                if not resp.ok:
                    raise RuntimeError(f"iLink {path} HTTP {resp.status}: {raw[:200]}")
                return json.loads(raw)

        return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)

    # ── persistence ───────────────────────────────────

    def _load_creds(self) -> None:
        path = self._state_dir / "creds.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._token = data.get("token", self._token)
            self._account_id = data.get("account_id", self._account_id)
            self._user_id = data.get("user_id", self._user_id)
        except Exception:
            pass

    def _save_creds(self) -> None:
        (self._state_dir / "creds.json").write_text(json.dumps({
            "token": self._token, "account_id": self._account_id, "user_id": self._user_id,
        }, indent=2), encoding="utf-8")

    def _load_sync_buf(self) -> None:
        path = self._state_dir / "sync.json"
        if path.exists():
            try:
                self._sync_buf = json.loads(path.read_text(encoding="utf-8")).get("get_updates_buf", "")
            except Exception:
                pass

    def _save_sync_buf(self) -> None:
        (self._state_dir / "sync.json").write_text(
            json.dumps({"get_updates_buf": self._sync_buf}), encoding="utf-8")


def _message_context_token(msg: dict) -> str:
    for key in ("context_token", "msg_context_token", "conversation_context_token"):
        value = msg.get(key)
        if value:
            return str(value)
    context = msg.get("context") or {}
    if isinstance(context, dict):
        value = context.get("context_token") or context.get("token")
        if value:
            return str(value)
    return ""


def _media_summary(item: dict) -> str:
    return _media_part(item).render_text()


def _media_part(item: dict) -> MessagePart:
    itype = item.get("type") or item.get("msg_type")
    kind = _media_kind(itype, item)
    data = _with_wechat_media_fields(_media_payload(item, kind))
    detail = _first_present(
        data,
        "file_name",
        "filename",
        "name",
        "file_id",
        "media_id",
        "url",
        "cdn_url",
        "aes_key",
        "md5",
    )
    return attachment_part(
        kind=kind,
        data=data,
        text=detail,
        name=str(data.get("file_name") or data.get("filename") or data.get("name") or ""),
        mime_type=str(data.get("mime_type") or data.get("mime") or ""),
        size=int(data.get("size") or data.get("file_size") or 0),
        url=str(data.get("url") or data.get("cdn_url") or ""),
        platform_file_id=str(data.get("file_id") or data.get("media_id") or ""),
        metadata_key="wechat_media",
    )


def _media_kind(itype, item: dict | None = None) -> str:
    if isinstance(item, dict):
        if isinstance(item.get("image_item"), dict):
            return "image"
        if isinstance(item.get("voice_item"), dict) or isinstance(item.get("audio_item"), dict):
            return "audio"
        if isinstance(item.get("video_item"), dict):
            return "video"
        if isinstance(item.get("file_item"), dict):
            return "file"
        if isinstance(item.get("media_item"), dict):
            return "file"
    mapping = {
        2: "image",
        3: "audio",
        4: "video",
        5: "file",
        "image": "image",
        "voice": "audio",
        "audio": "audio",
        "video": "video",
        "file": "file",
    }
    return mapping.get(itype, str(itype or "media"))


def _media_payload(item: dict, kind: str) -> dict:
    for key in (
        f"{kind}_item",
        "image_item",
        "voice_item",
        "audio_item",
        "video_item",
        "file_item",
        "media_item",
    ):
        value = item.get(key)
        if isinstance(value, dict):
            return value
    return item


def _with_wechat_media_fields(data: dict) -> dict:
    payload = dict(data or {})
    media = payload.get("media")
    encrypted_param = _first_present(payload, "encrypt_query_param", "encrypted_query_param")
    if isinstance(media, dict):
        encrypted_param = encrypted_param or _first_present(media, "encrypt_query_param", "encrypted_query_param")
        for source, target in (
            ("aes_key", "aes_key"),
            ("aeskey", "aes_key"),
            ("file_size", "file_size"),
            ("size", "size"),
            ("file_name", "file_name"),
            ("filename", "filename"),
            ("name", "name"),
            ("md5", "md5"),
        ):
            value = media.get(source)
            if value and not payload.get(target):
                payload[target] = value
    if encrypted_param and not payload.get("cdn_url") and not payload.get("url"):
        payload["cdn_url"] = _wechat_cdn_url(encrypted_param)
    if encrypted_param and not payload.get("file_id") and not payload.get("media_id"):
        payload["media_id"] = encrypted_param
    return payload


def _wechat_cdn_url(encrypted_query_param: str) -> str:
    return (
        "https://novac2c.cdn.weixin.qq.com/c2c/download"
        f"?encrypted_query_param={quote(str(encrypted_query_param), safe='')}"
    )


def _first_present(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value:
            return str(value)
    return ""


def _wechat_data(ref: AttachmentRef) -> dict[str, Any]:
    metadata = dict(ref.metadata or {})
    value = metadata.get("wechat_media")
    if isinstance(value, dict):
        return _with_wechat_media_fields(value)
    return {}


def _should_use_wechat_downloader(ref: AttachmentRef) -> bool:
    data = _wechat_data(ref)
    return bool(_wechat_is_encrypted(data) and _wechat_download_url(data))


def _wechat_download_url(data: dict[str, Any]) -> str:
    return _first_present(data, "download_url", "url", "cdn_url")


def _wechat_platform_file_id(ref: AttachmentRef) -> str:
    data = _wechat_data(ref)
    return (
        ref.platform_file_id
        or _first_present(data, "file_id", "media_id", "encrypt_query_param", "encrypted_query_param")
    )


def _wechat_is_encrypted(data: dict[str, Any]) -> bool:
    if _first_present(data, "aes_key", "aeskey"):
        return True
    if _first_present(data, "encrypt_query_param", "encrypted_query_param"):
        return True
    media = data.get("media")
    if isinstance(media, dict) and _first_present(media, "aes_key", "aeskey"):
        return True
    if isinstance(media, dict) and _first_present(media, "encrypt_query_param", "encrypted_query_param"):
        return True
    encrypt_type = str(data.get("encrypt_type") or data.get("encryptType") or "")
    return encrypt_type not in {"", "0", "none", "false", "False"}


def _wechat_aes_key(data: dict[str, Any]) -> bytes:
    value = _first_present(data, "aeskey", "aes_key")
    media = data.get("media")
    if not value and isinstance(media, dict):
        value = _first_present(media, "aeskey", "aes_key")
    if not value:
        return b""
    raw_value = value.strip()
    try:
        decoded = base64.b64decode(raw_value)
    except Exception:
        decoded = b""
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32 and _is_hex_bytes(decoded):
        return bytes.fromhex(decoded.decode("ascii"))
    if len(raw_value) == 32 and _is_hex_text(raw_value):
        return bytes.fromhex(raw_value)
    if len(raw_value.encode("utf-8")) == 16:
        return raw_value.encode("utf-8")
    raise AttachmentDownloadError("decrypt_key_invalid")


def _decrypt_wechat_payload(data: bytes, key: bytes) -> bytes:
    try:
        from Crypto.Cipher import AES
    except Exception as exc:
        raise AttachmentDownloadError("decrypt_unavailable", f"{type(exc).__name__}: {exc}") from exc
    if len(key) != 16:
        raise AttachmentDownloadError("decrypt_key_invalid")
    if len(data) % 16 != 0:
        raise AttachmentDownloadError("decrypt_payload_invalid")
    decrypted = AES.new(key, AES.MODE_ECB).decrypt(data)
    return _strip_pkcs7_padding(decrypted)


async def _read_limited_response(resp, limit: int) -> bytes:
    chunks = bytearray()
    async for chunk in resp.content.iter_chunked(64 * 1024):
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise AttachmentDownloadError("size_exceeded")
    return bytes(chunks)


def _strip_pkcs7_padding(data: bytes) -> bytes:
    if not data:
        raise AttachmentDownloadError("decrypt_payload_invalid")
    pad = data[-1]
    if pad < 1 or pad > 16:
        return data
    if data[-pad:] != bytes([pad]) * pad:
        return data
    return data[:-pad]


def _is_hex_bytes(value: bytes) -> bool:
    try:
        return _is_hex_text(value.decode("ascii"))
    except UnicodeDecodeError:
        return False


def _is_hex_text(value: str) -> bool:
    return all(char in "0123456789abcdefABCDEF" for char in value)


def _media_kind_from_ref(ref: AttachmentRef) -> str:
    return _media_kind_from_value(ref.kind)


def _media_kind_from_value(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"image", "photo", "picture", "img"}:
        return "image"
    if normalized in {"voice", "record", "audio", "sound"}:
        return "audio"
    if normalized in {"video", "movie"}:
        return "video"
    return "file"


def _wechat_download_name(ref: AttachmentRef, data: dict[str, Any]) -> str:
    value = (
        ref.name
        or _first_present(data, "file_name", "filename", "name")
        or Path(_wechat_download_url(data)).name
        or ref.platform_file_id
        or "attachment"
    )
    if Path(value).suffix:
        return value
    extension = mimetypes.guess_extension(ref.mime_type or str(data.get("mime_type") or "")) or ""
    return f"{value}{extension}"


# ── QR Helpers ──────────────────────────────────────

async def _fetch_qr(session, base_url: str) -> dict | None:
    """Fetch a new QR code. Returns {value, scan} or None."""
    async with session.get(
        f"{base_url}/ilink/bot/get_bot_qrcode?bot_type=3",
        headers={"iLink-App-Id": ILINK_APP_ID, "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION)},
    ) as resp:
        data = json.loads(await resp.text())
    value = str(data.get("qrcode") or "")
    url = str(data.get("qrcode_img_content") or "")
    if not value:
        return None
    return {"value": value, "scan": url or value}


def _print_qr(data: str) -> None:
    print("\n请用微信扫描以下二维码登录：\n")
    try:
        import qrcode
        qr = qrcode.QRCode()
        qr.add_data(data)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        print(f"链接: {data}")


# ── QR Login (CLI) ────────────────────────────────────

async def wechat_qr_login(state_dir: Path, base_url: str = API_BASE) -> dict | None:
    """Interactive QR login. Returns creds or None."""
    state_dir.mkdir(parents=True, exist_ok=True)
    t = aiohttp.ClientTimeout(total=None, connect=None, sock_connect=None, sock_read=None)

    async with aiohttp.ClientSession(trust_env=True, timeout=t) as session:
        qr = await _fetch_qr(session, base_url)
        if not qr:
            print("❌ 获取二维码失败。")
            return None
        qr_value, qr_scan = qr["value"], qr["scan"]
        _print_qr(qr_scan)
        refresh_count = 0

        for _ in range(480):
            await asyncio.sleep(1)
            async with session.get(
                f"{base_url}/ilink/bot/get_qrcode_status?qrcode={qr_value}",
                headers={"iLink-App-Id": ILINK_APP_ID, "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION)},
            ) as resp:
                status = json.loads(await resp.text())

            state = str(status.get("status") or status.get("state") or "")
            if state == "confirmed":
                creds = {
                    "token": str(status.get("bot_token") or ""),
                    "account_id": str(status.get("ilink_bot_id") or ""),
                    "user_id": str(status.get("ilink_user_id") or ""),
                }
                if not creds["token"] or not creds["account_id"]:
                    print("\n❌ 登录失败：凭证不完整。")
                    return None
                (state_dir / "creds.json").write_text(json.dumps(creds, indent=2), encoding="utf-8")
                print(f"\n✅ 登录成功！Account: {creds['account_id'][:12]}...")
                return creds
            elif state == "expired":
                qr_data = await _fetch_qr(session, base_url)
                if qr_data:
                    qr_value = qr_data["value"]
                    qr_scan = qr_data["scan"]
                    refresh_count += 1
                    if refresh_count > 3:
                        print("\n❌ 二维码多次过期，请重新运行 personal-agent wechat-login。")
                        return None
                    print(f"\n二维码已过期，正在刷新... ({refresh_count}/3)")
                    _print_qr(qr_scan)
                    continue
                return None
            elif state == "scaned":
                print("  已扫描，请在手机上确认...")
            # "wait" → continue polling

        print("\n❌ 登录超时（8分钟）。")
        return None
