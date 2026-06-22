"""SessionStore — two-layer: JSON index + SQLite messages."""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

from personal_agent.models.session import SessionEntry

logger = logging.getLogger(__name__)


class SessionStore:
    def __init__(self, db, data_dir: Path) -> None:
        self._db = db
        self._index_path = data_dir / "sessions.json"
        self._index: dict[str, SessionEntry] = {}

    async def initialize(self) -> None:
        self._load_index()

    # ── CRUD ──────────────────────────────────────────

    async def get_or_create(self, session_key: str, source) -> SessionEntry:
        if session_key in self._index:
            return self._index[session_key]

        entry = SessionEntry(
            session_id=str(uuid.uuid4()),
            session_key=session_key,
            platform=source.platform,
            user_id=source.user_id,
            user_name=getattr(source, "user_name", ""),
            chat_id=getattr(source, "chat_id", ""),
            chat_type=getattr(source, "chat_type", "dm"),
        )
        await self._db.create_session(entry)
        self._index[session_key] = entry
        self._save_index()
        logger.info("New session: %s → %s", session_key, entry.session_id)
        return entry

    async def load_history(self, session_id: str) -> list[dict]:
        return await self._db.load_history(session_id)

    async def save_transcript(self, session_id: str, messages: list[dict],
                              previous_count: int = 0) -> None:
        """Save new messages only (those after previous_count)."""
        new_msgs = messages[previous_count:]
        if not new_msgs:
            return
        from personal_agent.agent.finalize import finalize_turn
        # Simple inline finalize — skip full finalize_turn which needs ctx
        for msg in new_msgs:
            role = msg["role"]
            content = ""
            tool_calls = None
            tool_name = None
            tool_call_id = None
            if isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if block.get("type") == "text":
                        content += block.get("text", "")
                    elif block.get("type") == "tool_use":
                        tool_calls = tool_calls or []
                        tool_calls.append({"id": block.get("id"), "name": block.get("name"), "input": block.get("input", {})})
                        tool_name = block.get("name")
                    elif block.get("type") == "tool_result":
                        content = str(block.get("content", ""))
                        tool_call_id = block.get("tool_use_id", "")
            elif isinstance(msg.get("content"), str):
                content = msg["content"]
            await self._db.save_message(session_id, role, content, tool_calls, tool_name, tool_call_id)

        await self._db.update_last_active(session_id, increment_message=True)

    async def delete_session(self, session_key: str) -> str | None:
        entry = self._index.pop(session_key, None)
        if entry:
            await self._db.delete_session(entry.session_id)
            self._save_index()
            new_id = str(uuid.uuid4())
            logger.info("Session deleted: %s, new session will get ID %s", session_key, new_id)
            return new_id
        return None

    def get(self, session_key: str) -> SessionEntry | None:
        return self._index.get(session_key)

    # ── persistence ───────────────────────────────────

    def _load_index(self) -> None:
        if not self._index_path.exists():
            return
        try:
            data = json.loads(self._index_path.read_text())
            for key, val in data.items():
                self._index[key] = SessionEntry(**val)
            logger.info("Loaded %d sessions from index", len(self._index))
        except Exception:
            logger.exception("Failed to load sessions.json")

    def _save_index(self) -> None:
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: {
            "session_id": v.session_id, "session_key": v.session_key,
            "platform": v.platform, "user_id": v.user_id, "user_name": v.user_name,
            "chat_id": v.chat_id, "chat_type": v.chat_type,
            "created_at": v.created_at, "last_active_at": v.last_active_at,
            "message_count": v.message_count,
        } for k, v in self._index.items()}
        self._index_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
