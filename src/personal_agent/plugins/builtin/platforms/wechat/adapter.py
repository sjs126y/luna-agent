"""WeChat adapter — personal WeChat via Tencent iLink Bot API.

QR login → long-poll getupdates → sendmessage.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import struct
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

import aiohttp

from personal_agent.platforms.core import (
    BasePlatformAdapter,
    ChatInfo,
    PlatformCapabilities,
    SendResult,
)
from personal_agent.models.messages import MessageEnvelope, MessageEvent, MessagePart, SessionSource

logger = logging.getLogger(__name__)

API_BASE = "https://ilinkai.weixin.qq.com"
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
        typing=True,
        attachments_in=True,
        max_text_length=2000,
    )
    MAX_MESSAGE_LENGTH = 2000

    def __init__(self, config, db) -> None:
        super().__init__(config, db)
        self._token: str = getattr(config, "weixin_token", "") or ""
        self._account_id: str = getattr(config, "weixin_account_id", "") or ""
        self._user_id: str = getattr(config, "weixin_user_id", "") or ""
        self._base_url: str = getattr(config, "weixin_base_url", "") or API_BASE

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

    async def get_chat_info(self, chat_id: str) -> ChatInfo:
        return ChatInfo(chat_id=chat_id, chat_type="dm")

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
        """Split long text at paragraph boundaries, keeping code fences intact."""
        if len(text) <= self.MAX_MESSAGE_LENGTH:
            return [text]
        chunks = []
        current = ""
        in_fence = False
        for line in text.split("\n"):
            line = line.rstrip()
            if line.startswith("```"):
                in_fence = not in_fence
            if len(current) + len(line) >= self.MAX_MESSAGE_LENGTH and not in_fence:
                if current:
                    chunks.append(current.strip())
                current = line
            else:
                current = (current + "\n" + line).strip() if current else line
        if current:
            chunks.append(current.strip())
        return chunks if chunks else [text[:self.MAX_MESSAGE_LENGTH]]

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
                        asyncio.create_task(self._process_message(msg))
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

    async def _process_message(self, msg: dict) -> None:
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
    kind = _media_kind(itype)
    data = _media_payload(item, kind)
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
    return MessagePart(
        type=kind,
        text=detail,
        url=str(data.get("url") or data.get("cdn_url") or ""),
        file_id=str(data.get("file_id") or data.get("media_id") or ""),
        name=str(data.get("file_name") or data.get("filename") or data.get("name") or ""),
        metadata=dict(data),
    )


def _media_kind(itype) -> str:
    mapping = {
        2: "image",
        3: "voice",
        4: "video",
        5: "file",
        "image": "image",
        "voice": "voice",
        "audio": "voice",
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


def _first_present(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value:
            return str(value)
    return ""


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
