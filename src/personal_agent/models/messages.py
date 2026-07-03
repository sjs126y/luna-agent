"""Core message types — pure dataclasses, zero dependencies."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


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
