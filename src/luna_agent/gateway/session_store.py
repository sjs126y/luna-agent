"""SessionStore — two-layer: JSON index + SQLite messages."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from luna_agent.models.session import SessionEntry
from luna_agent.persistence.json_store import read_json_object, write_json_atomic

logger = logging.getLogger(__name__)


class SessionStore:
    def __init__(self, db, data_dir: Path, chain=None) -> None:
        self._db = db
        self._index_path = data_dir / "sessions.json"
        self._index: dict[str, SessionEntry] = {}
        self._chain = chain  # CompressionChain, optional

    async def initialize(self) -> None:
        self._load_index()

    # ── chain-aware session resolution ────────────────

    def resolve_session_id(self, session_key: str) -> str | None:
        """Walk the chain to find the latest session_id for this key."""
        entry = self._index.get(session_key)
        if entry is None:
            return None
        if self._chain:
            return self._chain.resolve(entry.session_id)
        return entry.session_id

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
        from luna_agent.agent.finalize import unpack_message
        for msg in new_msgs:
            role, content, tool_calls, tool_name, tool_call_id = unpack_message(msg)
            await self._db.save_message(session_id, role, content, tool_calls, tool_name, tool_call_id)

        await self._db.update_last_active(session_id, increment_message=True)

    async def reset_session(self, session_key: str, source=None) -> str:
        """Start a new conversation — keep old messages, create new session_id."""
        old = self._index.get(session_key)
        new_id = str(uuid.uuid4())
        entry = SessionEntry(
            session_id=new_id, session_key=session_key,
            platform=old.platform if old else getattr(source, "platform", ""),
            user_id=old.user_id if old else getattr(source, "user_id", ""),
            user_name=old.user_name if old else getattr(source, "user_name", ""),
            chat_id=old.chat_id if old else getattr(source, "chat_id", ""),
            chat_type=old.chat_type if old else getattr(source, "chat_type", "dm"),
            created_at=time.time(),
            last_active_at=time.time(),
        )
        await self._db.create_session(entry)
        self._index[session_key] = entry
        self._save_index()
        logger.info("Session reset: %s → %s", session_key, new_id)
        return new_id

    async def rename_session(self, old_key: str, new_key: str) -> bool:
        if old_key == new_key:
            return True
        if new_key in self._index:
            return False
        entry = self._index.pop(old_key, None)
        if entry is None:
            return False
        entry.session_key = new_key
        self._index[new_key] = entry
        await self._db.update_session_key(entry.session_id, new_key)
        self._save_index()
        logger.info("Session renamed: %s → %s", old_key, new_key)
        return True

    async def delete_session(self, session_key: str) -> str | None:
        entry = self._index.pop(session_key, None)
        if entry:
            session_ids = self._chain_ids(entry.session_id)
            for session_id in session_ids:
                await self._db.delete_session(session_id)
            if self._chain:
                self._chain.remove_chain(session_ids)
            self._save_index()
            new_id = str(uuid.uuid4())
            logger.info("Session deleted: %s, new session will get ID %s", session_key, new_id)
            return new_id
        return None

    async def create_compressed_session(self, session_key: str, source,
                                         compressed_messages: list[dict]) -> str:
        """Create a new session holding compressed messages, link to old via chain."""
        return await self.commit_compaction(session_key, source, compressed_messages)

    async def commit_compaction(
        self,
        session_key: str,
        source,
        replacement_history: list[dict],
    ) -> str:
        """Persist a replacement window before the active turn continues."""
        old_entry = self._index.get(session_key)
        if old_entry is None:
            return ""
        current_id = self._chain.resolve(old_entry.session_id) if self._chain else old_entry.session_id

        new_id = str(uuid.uuid4())
        entry = SessionEntry(
            session_id=new_id,
            session_key=session_key,
            platform=old_entry.platform,
            user_id=old_entry.user_id,
            user_name=old_entry.user_name,
            chat_id=old_entry.chat_id,
            chat_type=old_entry.chat_type,
            message_count=len(replacement_history),
        )
        try:
            await self._db.create_session(entry)

            from luna_agent.agent.finalize import unpack_message
            for msg in replacement_history:
                role, content, tool_calls, tool_name, tool_call_id = unpack_message(msg)
                await self._db.save_message(
                    new_id, role, content, tool_calls, tool_name, tool_call_id
                )

            if self._chain:
                self._chain.link(current_id, new_id)
        except Exception:
            await self._db.delete_session(new_id)
            raise

        logger.info("Compressed session: %s → %s (%d messages)",
                     current_id[:8], new_id[:8], len(replacement_history))
        return new_id

    def get(self, session_key: str) -> SessionEntry | None:
        return self._index.get(session_key)

    def entries(self) -> tuple[SessionEntry, ...]:
        """Return detached session metadata for routing restoration."""
        return tuple(replace(entry) for entry in self._index.values())

    def get_current_session_id(self, session_key: str) -> str | None:
        """Return the latest (uncompressed) session_id for this key."""
        entry = self._index.get(session_key)
        if entry is None:
            return None
        if self._chain:
            return self._chain.resolve(entry.session_id)
        return entry.session_id

    async def list_user_sessions(self, platform: str, user_id: str) -> list[dict]:
        """Return sessions matching platform + user_id, sorted by last active."""
        results = []
        for key, entry in self._index.items():
            if entry.platform == platform and entry.user_id == user_id:
                current_id = self._chain.resolve(entry.session_id) if self._chain else entry.session_id
                results.append({
                    "session_key": key,
                    "session_id": entry.session_id[:8],
                    "current_session_id": current_id,
                    "current_session_short": current_id[:8],
                    "message_count": await self._db.get_message_count(current_id),
                    "last_active": entry.last_active_at,
                })
        results.sort(key=lambda x: x.get("last_active", ""), reverse=True)
        return results

    async def export(self, session_id: str, output_path: str) -> int:
        """Export session as JSONL — user/assistant text only."""
        return await self._db.export_jsonl(session_id, output_path)

    async def save_tool_runs(self, runs: list[dict[str, Any]]) -> int:
        return await self._db.save_tool_runs(runs)

    async def recent_tool_runs(
        self,
        *,
        limit: int = 20,
        session_key: str | None = None,
        turn_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._db.recent_tool_runs(
            limit=limit,
            session_key=session_key,
            turn_id=turn_id,
        )

    async def get_tool_run(self, run_id: int) -> dict[str, Any] | None:
        return await self._db.get_tool_run(run_id)

    async def tool_run_summary(self, *, limit: int = 50) -> dict[str, Any]:
        return await self._db.tool_run_summary(limit=limit)

    async def save_turn_report(self, envelope: dict[str, Any]) -> int:
        return await self._db.save_turn_report(envelope)

    async def recent_turn_reports(
        self,
        *,
        limit: int = 20,
        session_key: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._db.recent_turn_reports(
            limit=limit,
            session_key=session_key,
            status=status,
        )

    async def get_turn_report(self, report_id: int) -> dict[str, Any] | None:
        return await self._db.get_turn_report(report_id)

    async def turn_report_summary(self) -> dict[str, Any]:
        return await self._db.turn_report_summary()

    async def expire_sessions(self, max_age_days: int = 30) -> int:
        """Remove sessions inactive for > max_age_days. Returns count removed."""
        import time
        cutoff = time.time() - (max_age_days * 86400)
        expired: list[str] = []

        for key, entry in list(self._index.items()):
            if entry.last_active_at < cutoff:
                expired.append(key)

        removed_ids: list[str] = []
        for key in expired:
            entry = self._index.pop(key, None)
            if entry:
                session_ids = self._chain_ids(entry.session_id)
                removed_ids.extend(session_ids)
                for session_id in session_ids:
                    await self._db.delete_session(session_id)

        if expired:
            self._save_index()
            if self._chain:
                self._chain.remove_chain(removed_ids)

        logger.info("Expired %d sessions (>%d days)", len(expired), max_age_days)
        return len(expired)

    def _chain_ids(self, session_id: str) -> list[str]:
        if self._chain:
            return self._chain.get_chain(session_id)
        return [session_id]

    # ── persistence ───────────────────────────────────

    def _load_index(self) -> None:
        if not self._index_path.exists():
            return
        data = read_json_object(self._index_path, {})
        for key, val in data.items():
            try:
                self._index[key] = SessionEntry(**val)
            except Exception:
                logger.exception("Failed to load session index entry: %s", key)
        logger.info("Loaded %d sessions from index", len(self._index))

    def _save_index(self) -> None:
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: {
            "session_id": v.session_id, "session_key": v.session_key,
            "platform": v.platform, "user_id": v.user_id, "user_name": v.user_name,
            "chat_id": v.chat_id, "chat_type": v.chat_type,
            "created_at": v.created_at, "last_active_at": v.last_active_at,
            "message_count": v.message_count,
        } for k, v in self._index.items()}
        write_json_atomic(self._index_path, data)
