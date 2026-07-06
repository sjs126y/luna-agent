"""SQLite persistence via aiosqlite. Serializes writes with an asyncio.Lock."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import time
from typing import Any

import aiosqlite

from personal_agent.text_safety import clean_payload, clean_text

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    session_key  TEXT NOT NULL,
    platform     TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    user_name    TEXT DEFAULT '',
    chat_id      TEXT DEFAULT '',
    chat_type    TEXT DEFAULT 'dm',
    created_at   REAL NOT NULL,
    last_active_at REAL NOT NULL,
    message_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sessions_key ON sessions(session_key);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL REFERENCES sessions(session_id),
    role          TEXT NOT NULL,
    content       TEXT DEFAULT '',
    tool_calls    TEXT DEFAULT NULL,
    tool_name     TEXT DEFAULT NULL,
    tool_call_id  TEXT DEFAULT NULL,
    timestamp     REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);

CREATE TABLE IF NOT EXISTS tool_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT NOT NULL REFERENCES sessions(session_id),
    session_key         TEXT NOT NULL,
    turn_id             TEXT DEFAULT '',
    tool_use_id         TEXT NOT NULL,
    tool_name           TEXT NOT NULL,
    status              TEXT NOT NULL,
    category            TEXT DEFAULT '',
    duration            REAL DEFAULT 0,
    input_summary       TEXT DEFAULT '',
    output_summary      TEXT DEFAULT '',
    full_output         TEXT DEFAULT '',
    output_truncated    INTEGER DEFAULT 0,
    error               TEXT DEFAULT '',
    guard_stage         TEXT DEFAULT '',
    reason_code         TEXT DEFAULT '',
    permission_category TEXT DEFAULT '',
    permission_decision TEXT DEFAULT '',
    required_allow      TEXT DEFAULT '',
    execution_mode      TEXT DEFAULT '',
    grant_matched       TEXT DEFAULT '',
    created_at          REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tool_runs_session ON tool_runs(session_id, id);
CREATE INDEX IF NOT EXISTS idx_tool_runs_session_key ON tool_runs(session_key, id);
CREATE INDEX IF NOT EXISTS idx_tool_runs_turn ON tool_runs(turn_id, id);
CREATE INDEX IF NOT EXISTS idx_tool_runs_status ON tool_runs(status, id);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._write_lock = asyncio.Lock()
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        logger.info("Database initialized at %s", self._path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ── sessions ──────────────────────────────────────

    async def create_session_direct(self, session_id: str, session_key: str) -> None:
        """Minimal session creation for tests. Skips the full SessionEntry object."""
        async with self._write_lock:
            await self._conn.execute(
                "INSERT INTO sessions (session_id, session_key, platform, user_id, created_at, last_active_at) "
                "VALUES (?, ?, 'test', '', ?, ?)",
                (session_id, session_key, time.time(), time.time()),
            )
            await self._conn.commit()

    async def create_session(self, entry) -> None:
        async with self._write_lock:
            await self._conn.execute(
                """INSERT INTO sessions (session_id, session_key, platform, user_id,
                   user_name, chat_id, chat_type, created_at, last_active_at, message_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (entry.session_id, entry.session_key, entry.platform, entry.user_id,
                 entry.user_name, entry.chat_id, entry.chat_type,
                 entry.created_at, entry.last_active_at, entry.message_count),
            )
            await self._conn.commit()

    async def update_last_active(self, session_id: str, increment_message: bool = True) -> None:
        async with self._write_lock:
            if increment_message:
                await self._conn.execute(
                    "UPDATE sessions SET last_active_at = ?, message_count = message_count + 1 WHERE session_id = ?",
                    (time.time(), session_id),
                )
            else:
                await self._conn.execute(
                    "UPDATE sessions SET last_active_at = ? WHERE session_id = ?",
                    (time.time(), session_id),
                )
            await self._conn.commit()

    async def delete_session(self, session_id: str) -> None:
        async with self._write_lock:
            await self._conn.execute("DELETE FROM tool_runs WHERE session_id = ?", (session_id,))
            await self._conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            await self._conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            await self._conn.commit()

    async def update_session_key(self, session_id: str, session_key: str) -> None:
        async with self._write_lock:
            await self._conn.execute(
                "UPDATE sessions SET session_key = ? WHERE session_id = ?",
                (session_key, session_id),
            )
            await self._conn.commit()

    async def get_session_key(self, session_id: str) -> str | None:
        rows = await self._conn.execute(
            "SELECT session_key FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        async for row in rows:
            return row["session_key"]
        return None

    # ── messages ──────────────────────────────────────

    async def save_message(
        self, session_id: str, role: str, content: str = "",
        tool_calls: list[dict] | None = None,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        content = clean_text(content)
        tc_json = json.dumps(clean_payload(tool_calls)) if tool_calls else None
        async with self._write_lock:
            await self._conn.execute(
                """INSERT INTO messages (session_id, role, content, tool_calls, tool_name, tool_call_id, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session_id, role, content, tc_json, tool_name, tool_call_id, time.time()),
            )
            await self._conn.commit()

    async def load_history(self, session_id: str) -> list[dict]:
        """Load conversation history. Tool messages are converted to plain text.

        Anthropic API requires tool_use blocks in assistant messages to be
        immediately followed by tool_result blocks in a user message. If stored
        history were replayed as-is, missing or misordered tool_results cause
        400 errors. Instead, we convert tool interactions to natural text.
        """
        rows = await self._conn.execute(
            "SELECT role, content, tool_name, tool_call_id "
            "FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
        messages: list[dict] = []
        async for row in rows:
            role = row["role"]
            text = (row["content"] or "").strip()
            text = clean_text(text).strip()

            if row["tool_name"]:
                # Assistant message with tool_use — keep text, skip tool_use block
                if text:
                    messages.append({
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                    })
                # Pure tool_use (no text) → skip entirely
                continue

            if row["tool_call_id"]:
                # Tool_result → convert to plain user text
                if text:
                    messages.append({
                        "role": "user",
                        "content": [{"type": "text", "text": text}],
                    })
                continue

            # Normal text message
            if text:
                messages.append({
                    "role": role,
                    "content": [{"type": "text", "text": text}],
                })

        return messages

    async def get_message_count(self, session_id: str) -> int:
        row = await self._conn.execute(
            "SELECT COUNT(*) as cnt FROM messages WHERE session_id = ?", (session_id,)
        )
        async for r in row:
            return r["cnt"]
        return 0

    async def export_jsonl(self, session_id: str, output_path: str) -> int:
        """Export session as JSONL — user/assistant text only, no tool calls.

        Each line: {"role": "user|assistant", "content": "text"}
        Returns the number of messages exported.
        """
        rows = await self._conn.execute(
            "SELECT role, content, tool_calls, tool_name, tool_call_id "
            "FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
        from pathlib import Path
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with open(output_path, "w", encoding="utf-8") as f:
            async for row in rows:
                role = row["role"]
                # Skip tool messages
                if row["tool_call_id"] or row["tool_name"]:
                    continue
                # Extract text from content blocks
                try:
                    blocks = json.loads(row["content"])
                    if isinstance(blocks, list):
                        texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
                        content = " ".join(texts)
                    else:
                        content = str(blocks)
                except (json.JSONDecodeError, TypeError):
                    content = str(row["content"])
                if not content.strip():
                    continue
                f.write(json.dumps({"role": role, "content": content}, ensure_ascii=False) + "\n")
                count += 1
        return count

    # ── tool runs ─────────────────────────────────────

    async def save_tool_runs(self, runs: list[dict[str, Any]]) -> int:
        if not runs:
            return 0
        rows = [
            (
                str(run.get("session_id") or ""),
                str(run.get("session_key") or ""),
                str(run.get("turn_id") or ""),
                str(run.get("tool_use_id") or ""),
                str(run.get("tool_name") or ""),
                str(run.get("status") or ""),
                str(run.get("category") or ""),
                _as_float(run.get("duration")),
                clean_text(str(run.get("input_summary") or "")),
                clean_text(str(run.get("output_summary") or "")),
                clean_text(str(run.get("full_output") or "")),
                1 if run.get("output_truncated") else 0,
                clean_text(str(run.get("error") or "")),
                str(run.get("guard_stage") or ""),
                str(run.get("reason_code") or ""),
                str(run.get("permission_category") or ""),
                str(run.get("permission_decision") or ""),
                str(run.get("required_allow") or ""),
                str(run.get("execution_mode") or ""),
                str(run.get("grant_matched") or ""),
                _as_float(run.get("created_at")) or time.time(),
            )
            for run in runs
        ]
        async with self._write_lock:
            await self._conn.executemany(
                """INSERT INTO tool_runs (
                    session_id, session_key, turn_id, tool_use_id, tool_name,
                    status, category, duration, input_summary, output_summary,
                    full_output, output_truncated, error, guard_stage, reason_code,
                    permission_category, permission_decision, required_allow,
                    execution_mode, grant_matched, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            await self._conn.commit()
        return len(rows)

    async def recent_tool_runs(
        self,
        *,
        limit: int = 20,
        session_key: str | None = None,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        params: list[Any] = []
        where = ""
        if session_key:
            where = "WHERE session_key = ?"
            params.append(session_key)
        params.append(int(limit))
        rows = await self._conn.execute(
            f"""SELECT * FROM tool_runs
                {where}
                ORDER BY id DESC
                LIMIT ?""",
            params,
        )
        items: list[dict[str, Any]] = []
        async for row in rows:
            items.append(_tool_run_row(row))
        items.reverse()
        return items

    async def get_tool_run(self, run_id: int) -> dict[str, Any] | None:
        rows = await self._conn.execute(
            "SELECT * FROM tool_runs WHERE id = ?",
            (run_id,),
        )
        async for row in rows:
            return _tool_run_row(row)
        return None

    async def tool_run_summary(self, *, limit: int = 50) -> dict[str, Any]:
        if limit <= 0:
            return _empty_tool_run_summary()
        rows = await self._conn.execute(
            """SELECT tool_name, status, category, output_truncated
               FROM tool_runs
               ORDER BY id DESC
               LIMIT ?""",
            (int(limit),),
        )
        inspected = 0
        tool_counts: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        category_counts: dict[str, int] = {}
        truncated = 0
        async for row in rows:
            inspected += 1
            tool_name = str(row["tool_name"] or "")
            status = str(row["status"] or "")
            category = str(row["category"] or "")
            if tool_name:
                tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
            if status:
                status_counts[status] = status_counts.get(status, 0) + 1
            if category:
                category_counts[category] = category_counts.get(category, 0) + 1
            if row["output_truncated"]:
                truncated += 1
        return {
            "inspected": inspected,
            "tool_counts": dict(sorted(tool_counts.items())),
            "status_counts": dict(sorted(status_counts.items())),
            "category_counts": dict(sorted(category_counts.items())),
            "denied": int(status_counts.get("denied", 0)),
            "failed": int(status_counts.get("error", 0)),
            "timeouts": int(status_counts.get("timeout", 0)),
            "truncated": truncated,
        }


def _tool_run_row(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "session_id": str(row["session_id"] or ""),
        "session_key": str(row["session_key"] or ""),
        "turn_id": str(row["turn_id"] or ""),
        "tool_use_id": str(row["tool_use_id"] or ""),
        "tool_name": str(row["tool_name"] or ""),
        "status": str(row["status"] or ""),
        "category": str(row["category"] or ""),
        "duration": float(row["duration"] or 0.0),
        "input_summary": str(row["input_summary"] or ""),
        "output_summary": str(row["output_summary"] or ""),
        "full_output": str(row["full_output"] or ""),
        "output_truncated": bool(row["output_truncated"]),
        "error": str(row["error"] or ""),
        "guard_stage": str(row["guard_stage"] or ""),
        "reason_code": str(row["reason_code"] or ""),
        "permission_category": str(row["permission_category"] or ""),
        "permission_decision": str(row["permission_decision"] or ""),
        "required_allow": str(row["required_allow"] or ""),
        "execution_mode": str(row["execution_mode"] or ""),
        "grant_matched": str(row["grant_matched"] or ""),
        "created_at": float(row["created_at"] or 0.0),
    }


def _empty_tool_run_summary() -> dict[str, Any]:
    return {
        "inspected": 0,
        "tool_counts": {},
        "status_counts": {},
        "category_counts": {},
        "denied": 0,
        "failed": 0,
        "timeouts": 0,
        "truncated": 0,
    }


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
