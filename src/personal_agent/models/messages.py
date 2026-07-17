"""Core message types — pure dataclasses, zero dependencies."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class MessagePart:
    """Structured platform message unit with a stable text fallback."""
    type: str = "text"
    text: str = ""
    url: str = ""
    path: str = ""
    file_id: str = ""
    artifact_id: str = ""
    name: str = ""
    mime_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def render_text(self) -> str:
        if self.type == "text":
            return self.text
        if self.type == "mention":
            return f"@{self.text or self.name or self.file_id}".strip()
        if self.type == "quote":
            target = self.file_id or self.text or self.name
            return f"[reply:{target}]" if target else "[reply]"
        detail = self.name or self.artifact_id or self.url or self.path or self.file_id or self.text
        return f"[{self.type}: {detail}]" if detail else f"[{self.type}]"

    def to_attachment_ref(self, attachment_id: str = "") -> "AttachmentRef":
        detail = self.file_id or self.url or self.path or self.name or self.text
        size = 0
        try:
            size = int(self.metadata.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return AttachmentRef(
            id=attachment_id or detail,
            kind=self.type,
            name=self.name,
            mime_type=self.mime_type,
            size=size,
            url=self.url,
            platform_file_id=self.file_id,
            local_path=self.path,
            metadata=dict(self.metadata),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "text": self.text,
            "url": self.url,
            "path": self.path,
            "file_id": self.file_id,
            "artifact_id": self.artifact_id,
            "name": self.name,
            "mime_type": self.mime_type,
            "metadata": dict(self.metadata),
        }


@dataclass
class OutboundMessage:
    """Structured outbound platform message with a text-compatible fallback."""
    parts: list[MessagePart] = field(default_factory=list)

    @classmethod
    def text(cls, value: str) -> "OutboundMessage":
        return cls(parts=[MessagePart(type="text", text=str(value or ""))])

    def render_text(self) -> str:
        if not self.parts:
            return ""
        return "".join(part.render_text() for part in self.parts)

    def text_content(self) -> str:
        return "".join(part.text for part in self.parts if part.type == "text")

    def as_dict(self) -> dict[str, Any]:
        return {"parts": [part.as_dict() for part in self.parts]}


@dataclass(frozen=True)
class PlatformCapabilities:
    text: bool = True
    markdown: bool = False
    rich_text: bool = False
    image_send: bool = False
    file_send: bool = False
    audio_send: bool = False
    video_send: bool = False
    mention: bool = False
    reply: bool = False
    typing: bool = False
    attachments_in: bool = False
    max_text_length: int = 0
    max_file_bytes: int = 0
    max_attachments: int = 0
    media_caption: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "markdown": self.markdown,
            "rich_text": self.rich_text,
            "image_send": self.image_send,
            "file_send": self.file_send,
            "audio_send": self.audio_send,
            "video_send": self.video_send,
            "mention": self.mention,
            "reply": self.reply,
            "typing": self.typing,
            "attachments_in": self.attachments_in,
            "max_text_length": self.max_text_length,
            "max_file_bytes": self.max_file_bytes,
            "max_attachments": self.max_attachments,
            "media_caption": self.media_caption,
        }


@dataclass
class SessionSource:
    """Identifies where a message came from."""
    platform: str              # "feishu" | "telegram"
    user_id: str               # platform-specific user id
    user_name: str = ""
    chat_id: str = ""          # platform-specific chat id
    chat_type: str = "dm"      # "dm" | "group" | "channel"
    thread_id: str | None = None


@dataclass
class AttachmentRef:
    """Stable reference for an attachment carried by a platform message."""
    id: str
    kind: str
    name: str = ""
    mime_type: str = ""
    size: int = 0
    url: str = ""
    platform_file_id: str = ""
    local_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "mime_type": self.mime_type,
            "size": self.size,
            "url": self.url,
            "platform_file_id": self.platform_file_id,
            "local_path": self.local_path,
            "metadata": dict(self.metadata),
        }


@dataclass
class MessageEnvelope:
    """Hermes-style normalized platform message envelope."""
    id: str = ""
    source: SessionSource = field(default_factory=lambda: SessionSource(platform="", user_id=""))
    text: str = ""
    parts: list[MessagePart] = field(default_factory=list)
    attachments: list[AttachmentRef] = field(default_factory=list)
    reply_to: str = ""
    thread_id: str | None = None
    raw: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def render_text(self) -> str:
        if self.text:
            return self.text
        return "".join(part.render_text() for part in self.parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": {
                "platform": self.source.platform,
                "user_id": self.source.user_id,
                "user_name": self.source.user_name,
                "chat_id": self.source.chat_id,
                "chat_type": self.source.chat_type,
                "thread_id": self.source.thread_id,
            },
            "text": self.text,
            "parts": [part.as_dict() for part in self.parts],
            "attachments": [attachment.as_dict() for attachment in self.attachments],
            "reply_to": self.reply_to,
            "thread_id": self.thread_id,
            "metadata": dict(self.metadata),
        }


@dataclass
class ResponseEnvelope:
    """Structured outbound response envelope for future platform renderers."""
    text: str = ""
    parts: list[MessagePart] = field(default_factory=list)
    attachments: list[AttachmentRef] = field(default_factory=list)
    reply_to: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def render_text(self) -> str:
        if self.text:
            return self.text
        return "".join(part.render_text() for part in self.parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "parts": [part.as_dict() for part in self.parts],
            "attachments": [attachment.as_dict() for attachment in self.attachments],
            "reply_to": self.reply_to,
            "metadata": dict(self.metadata),
        }


@dataclass
class MessageEvent:
    """Normalized incoming message from any platform."""
    text: str
    message_type: str = "text"       # "text" | "command"
    source: SessionSource = field(default_factory=lambda: SessionSource(platform="", user_id=""))
    parts: list[MessagePart] = field(default_factory=list)
    attachments: list[MessagePart] = field(default_factory=list)
    envelope: MessageEnvelope | None = None
    raw_message: Any = None          # original platform-specific object
    message_id: str | None = None
    reply_to_text: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    internal: bool = False           # system-generated, skip auth

    def to_envelope(self) -> MessageEnvelope:
        if self.envelope is not None:
            return self.envelope
        parts = list(self.parts)
        if not parts and self.text:
            parts = [MessagePart(type="text", text=self.text)]
        attachments = [
            part.to_attachment_ref(_attachment_id(self.message_id, index))
            for index, part in enumerate(self.attachments, start=1)
        ]
        self.envelope = MessageEnvelope(
            id=str(self.message_id or ""),
            source=self.source,
            text=self.text,
            parts=parts,
            attachments=attachments,
            reply_to=self.reply_to_text or "",
            thread_id=self.source.thread_id,
            raw=self.raw_message,
            metadata={
                "message_type": self.message_type,
                "internal": self.internal,
            },
        )
        return self.envelope


def _attachment_id(message_id: str | None, index: int) -> str:
    prefix = str(message_id or "attachment").strip() or "attachment"
    return f"{prefix}:{index}"


@dataclass
class NormalizedResponse:
    """Unified LLM response, regardless of provider."""
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)  # [{"id":..., "name":..., "input":{}}]
    usage: dict = field(default_factory=dict)              # {"input_tokens": N, "output_tokens": M}
    finish_reason: str = ""                                 # "end_turn" | "tool_use" | "max_tokens" | "stop"
    stop_reason: str = ""                                   # raw from API
    model: str = ""
    thinking: str = ""                                      # reasoning/thinking text (streamed separately)
