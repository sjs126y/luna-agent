"""Session data model."""

from dataclasses import dataclass, field
import time


@dataclass
class SessionEntry:
    session_id: str          # UUID, DB primary key
    session_key: str         # "feishu:chat_id:user_id" — composite routing key
    platform: str            # "feishu" | "telegram"
    user_id: str             # platform-specific user id
    user_name: str = ""
    chat_id: str = ""        # routing target for replies
    chat_type: str = "dm"    # "dm" | "group"
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    message_count: int = 0
