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
        detail = self.name or self.url or self.path or self.file_id or self.text
        return f"[{self.type}: {detail}]" if detail else f"[{self.type}]"

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "text": self.text,
            "url": self.url,
            "path": self.path,
            "file_id": self.file_id,
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
class MessageEvent:
    """Normalized incoming message from any platform."""
    text: str
    message_type: str = "text"       # "text" | "command"
    source: SessionSource = field(default_factory=lambda: SessionSource(platform="", user_id=""))
    parts: list[MessagePart] = field(default_factory=list)
    attachments: list[MessagePart] = field(default_factory=list)
    raw_message: Any = None          # original platform-specific object
    message_id: str | None = None
    reply_to_text: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    internal: bool = False           # system-generated, skip auth


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
