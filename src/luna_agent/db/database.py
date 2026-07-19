"""SQLite persistence via aiosqlite. Serializes writes with an asyncio.Lock."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import time
from typing import Any

import aiosqlite

from luna_agent.text_safety import clean_payload, clean_text

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

CREATE TABLE IF NOT EXISTS compression_checkpoints (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key           TEXT NOT NULL,
    source_session_id     TEXT NOT NULL,
    target_session_id     TEXT NOT NULL REFERENCES sessions(session_id),
    window_number         INTEGER NOT NULL,
    window_id             TEXT NOT NULL UNIQUE,
    first_window_id       TEXT NOT NULL,
    previous_window_id    TEXT NOT NULL,
    trigger               TEXT DEFAULT 'auto',
    model                 TEXT DEFAULT '',
    pre_tokens            INTEGER DEFAULT 0,
    post_tokens           INTEGER DEFAULT 0,
    summary_tokens        INTEGER DEFAULT 0,
    retained_user_tokens  INTEGER DEFAULT 0,
    pre_message_count     INTEGER DEFAULT 0,
    post_message_count    INTEGER DEFAULT 0,
    details_json          TEXT DEFAULT '{}',
    created_at            REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_compaction_checkpoints_session_key
ON compression_checkpoints(session_key, id);

CREATE INDEX IF NOT EXISTS idx_compaction_checkpoints_target
ON compression_checkpoints(target_session_id);

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
    artifact_summary_json TEXT DEFAULT '[]',
    result_metadata_json TEXT DEFAULT '{}',
    error               TEXT DEFAULT '',
    guard_stage         TEXT DEFAULT '',
    reason_code         TEXT DEFAULT '',
    permission_category TEXT DEFAULT '',
    permission_decision TEXT DEFAULT '',
    required_allow      TEXT DEFAULT '',
    execution_mode      TEXT DEFAULT '',
    grant_matched       TEXT DEFAULT '',
    grant_scope         TEXT DEFAULT '',
    grant_expires_at    REAL DEFAULT 0,
    temporary_grant_ttl_seconds INTEGER DEFAULT 0,
    created_at          REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tool_runs_session ON tool_runs(session_id, id);
CREATE INDEX IF NOT EXISTS idx_tool_runs_session_key ON tool_runs(session_key, id);
CREATE INDEX IF NOT EXISTS idx_tool_runs_turn ON tool_runs(turn_id, id);
CREATE INDEX IF NOT EXISTS idx_tool_runs_status ON tool_runs(status, id);

CREATE TABLE IF NOT EXISTS turn_reports (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id             TEXT NOT NULL REFERENCES sessions(session_id),
    session_key            TEXT NOT NULL,
    turn_id                TEXT DEFAULT '',
    status                 TEXT NOT NULL,
    completed              INTEGER DEFAULT 0,
    duration               REAL DEFAULT 0,
    error                  TEXT DEFAULT '',
    user_message_summary   TEXT DEFAULT '',
    final_response_summary TEXT DEFAULT '',
    llm_calls              INTEGER DEFAULT 0,
    tool_calls             INTEGER DEFAULT 0,
    cache_hit_tokens       INTEGER DEFAULT 0,
    cache_miss_tokens      INTEGER DEFAULT 0,
    cache_write_tokens     INTEGER DEFAULT 0,
    cache_read_tokens      INTEGER DEFAULT 0,
    source_json            TEXT DEFAULT '{}',
    report_json            TEXT NOT NULL,
    created_at             REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_turn_reports_session ON turn_reports(session_id, id);
CREATE INDEX IF NOT EXISTS idx_turn_reports_session_key ON turn_reports(session_key, id);
CREATE INDEX IF NOT EXISTS idx_turn_reports_turn ON turn_reports(turn_id);
CREATE INDEX IF NOT EXISTS idx_turn_reports_status ON turn_reports(status, id);

CREATE TABLE IF NOT EXISTS delivery_outbox (
    delivery_id   TEXT PRIMARY KEY,
    session_key   TEXT NOT NULL,
    kind          TEXT NOT NULL,
    message_json  TEXT NOT NULL,
    metadata_json TEXT DEFAULT '{}',
    status        TEXT NOT NULL,
    attempts      INTEGER DEFAULT 0,
    next_attempt_at REAL DEFAULT 0,
    platform      TEXT DEFAULT '',
    chat_id       TEXT DEFAULT '',
    message_id    TEXT DEFAULT '',
    last_error    TEXT DEFAULT '',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_delivery_outbox_due
ON delivery_outbox(status, next_attempt_at, created_at);

CREATE TABLE IF NOT EXISTS delivery_outbox_parts (
    delivery_id     TEXT NOT NULL REFERENCES delivery_outbox(delivery_id) ON DELETE CASCADE,
    part_index      INTEGER NOT NULL,
    operation_json  TEXT NOT NULL,
    artifact_id     TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER DEFAULT 0,
    platform_file_id TEXT DEFAULT '',
    message_id      TEXT DEFAULT '',
    last_error      TEXT DEFAULT '',
    ambiguous       INTEGER DEFAULT 0,
    updated_at      REAL NOT NULL,
    PRIMARY KEY (delivery_id, part_index)
);

CREATE INDEX IF NOT EXISTS idx_delivery_parts_status
ON delivery_outbox_parts(delivery_id, status, part_index);

CREATE TABLE IF NOT EXISTS submission_ledger (
    scope          TEXT NOT NULL,
    owner_id       TEXT NOT NULL,
    request_id     TEXT NOT NULL,
    session_key    TEXT NOT NULL,
    payload_hash   TEXT NOT NULL,
    response_mode  TEXT NOT NULL,
    status         TEXT NOT NULL,
    kind           TEXT DEFAULT 'conversation',
    turn_id        TEXT DEFAULT '',
    delivery_id    TEXT DEFAULT '',
    response       TEXT DEFAULT '',
    message_json   TEXT DEFAULT '',
    error          TEXT DEFAULT '',
    attempts       INTEGER DEFAULT 1,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    completed_at   REAL DEFAULT 0,
    PRIMARY KEY (scope, owner_id, request_id)
);

CREATE INDEX IF NOT EXISTS idx_submission_ledger_status
ON submission_ledger(status, updated_at);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id       TEXT PRIMARY KEY,
    session_key       TEXT NOT NULL,
    turn_id           TEXT NOT NULL,
    owner_id          TEXT DEFAULT '',
    kind              TEXT NOT NULL,
    filename          TEXT NOT NULL,
    mime_type         TEXT DEFAULT '',
    size_bytes        INTEGER NOT NULL,
    content_hash      TEXT NOT NULL,
    relative_path     TEXT NOT NULL,
    source            TEXT DEFAULT 'tool',
    source_name       TEXT DEFAULT '',
    status            TEXT DEFAULT 'candidate',
    delivery_eligible INTEGER DEFAULT 1,
    truncated         INTEGER DEFAULT 0,
    metadata_json     TEXT DEFAULT '{}',
    created_at        REAL NOT NULL,
    expires_at        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_turn
ON artifacts(session_key, turn_id, created_at);

CREATE INDEX IF NOT EXISTS idx_artifacts_expiry
ON artifacts(status, expires_at);
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
        await self._ensure_tool_run_columns()
        await self._ensure_delivery_part_columns()
        await self._conn.execute(
            "UPDATE delivery_outbox SET status = 'retry', next_attempt_at = 0 "
            "WHERE status = 'sending'"
        )
        await self._conn.execute(
            "UPDATE delivery_outbox_parts SET status = 'retry' "
            "WHERE status = 'sending'"
        )
        await self._conn.execute(
            "UPDATE submission_ledger SET status = 'retryable' "
            "WHERE status IN ('accepted', 'running')"
        )
        await self._conn.commit()
        logger.info("Database initialized at %s", self._path)

    async def _ensure_tool_run_columns(self) -> None:
        rows = await self._conn.execute("PRAGMA table_info(tool_runs)")
        columns = {row["name"] async for row in rows}
        for name, ddl in {
            "grant_scope": "ALTER TABLE tool_runs ADD COLUMN grant_scope TEXT DEFAULT ''",
            "grant_expires_at": "ALTER TABLE tool_runs ADD COLUMN grant_expires_at REAL DEFAULT 0",
            "temporary_grant_ttl_seconds": "ALTER TABLE tool_runs ADD COLUMN temporary_grant_ttl_seconds INTEGER DEFAULT 0",
            "artifact_summary_json": "ALTER TABLE tool_runs ADD COLUMN artifact_summary_json TEXT DEFAULT '[]'",
            "result_metadata_json": "ALTER TABLE tool_runs ADD COLUMN result_metadata_json TEXT DEFAULT '{}'",
        }.items():
            if name not in columns:
                await self._conn.execute(ddl)

    async def _ensure_delivery_part_columns(self) -> None:
        rows = await self._conn.execute("PRAGMA table_info(delivery_outbox_parts)")
        columns = {row["name"] async for row in rows}
        if "artifact_id" not in columns:
            await self._conn.execute(
                "ALTER TABLE delivery_outbox_parts ADD COLUMN artifact_id TEXT DEFAULT ''"
            )

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ── durable submissions ───────────────────────

    async def claim_submission(self, values: tuple) -> tuple[dict, bool]:
        """Insert a submission claim or atomically reclaim an interrupted claim."""
        scope, owner_id, request_id = values[:3]
        async with self._write_lock:
            cursor = await self._conn.execute(
                """INSERT OR IGNORE INTO submission_ledger
                   (scope, owner_id, request_id, session_key, payload_hash,
                    response_mode, status, kind, turn_id, delivery_id, response,
                    message_json, error, attempts, created_at, updated_at, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            owned = cursor.rowcount == 1
            if not owned:
                reclaim = await self._conn.execute(
                    """UPDATE submission_ledger
                       SET status = 'accepted', attempts = attempts + 1, updated_at = ?
                       WHERE scope = ? AND owner_id = ? AND request_id = ?
                         AND payload_hash = ? AND status = 'retryable'""",
                    (time.time(), scope, owner_id, request_id, values[4]),
                )
                owned = reclaim.rowcount == 1
            await self._conn.commit()
            row_cursor = await self._conn.execute(
                """SELECT * FROM submission_ledger
                   WHERE scope = ? AND owner_id = ? AND request_id = ?""",
                (scope, owner_id, request_id),
            )
            row = await row_cursor.fetchone()
            return dict(row), owned

    async def submission_record(
        self,
        scope: str,
        owner_id: str,
        request_id: str,
    ) -> dict | None:
        cursor = await self._conn.execute(
            """SELECT * FROM submission_ledger
               WHERE scope = ? AND owner_id = ? AND request_id = ?""",
            (scope, owner_id, request_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_submission(
        self,
        scope: str,
        owner_id: str,
        request_id: str,
        **changes,
    ) -> None:
        allowed = {
            "status", "kind", "turn_id", "delivery_id", "response",
            "message_json", "error", "updated_at", "completed_at",
        }
        values = {key: value for key, value in changes.items() if key in allowed}
        if not values:
            return
        assignments = ", ".join(f"{key} = ?" for key in values)
        async with self._write_lock:
            await self._conn.execute(
                f"UPDATE submission_ledger SET {assignments} "
                "WHERE scope = ? AND owner_id = ? AND request_id = ?",
                (*values.values(), scope, owner_id, request_id),
            )
            await self._conn.commit()

    async def prune_submissions(self, *, before: float) -> int:
        async with self._write_lock:
            cursor = await self._conn.execute(
                """DELETE FROM submission_ledger
                   WHERE completed_at > 0 AND completed_at < ?
                     AND status IN ('completed', 'delivery_pending', 'failed', 'cancelled', 'rejected')""",
                (before,),
            )
            await self._conn.commit()
            return int(cursor.rowcount or 0)

    # ── delivery outbox ───────────────────────────────

    async def enqueue_delivery(self, values: tuple) -> None:
        async with self._write_lock:
            await self._conn.execute(
                """INSERT OR IGNORE INTO delivery_outbox
                   (delivery_id, session_key, kind, message_json, metadata_json,
                    status, attempts, next_attempt_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            await self._conn.commit()

    async def due_deliveries(self, *, now: float, limit: int = 50) -> list[dict]:
        cursor = await self._conn.execute(
            """SELECT * FROM delivery_outbox
               WHERE status IN ('pending', 'retry') AND next_attempt_at <= ?
               ORDER BY created_at LIMIT ?""",
            (now, max(1, int(limit))),
        )
        return [dict(row) async for row in cursor]

    async def delivery_record(self, delivery_id: str) -> dict | None:
        cursor = await self._conn.execute(
            "SELECT * FROM delivery_outbox WHERE delivery_id = ?",
            (delivery_id,),
        )
        async for row in cursor:
            return dict(row)
        return None

    async def update_delivery(self, delivery_id: str, **changes) -> None:
        allowed = {
            "status", "attempts", "next_attempt_at", "platform", "chat_id",
            "message_id", "last_error", "updated_at",
        }
        values = {key: value for key, value in changes.items() if key in allowed}
        if not values:
            return
        assignments = ", ".join(f"{key} = ?" for key in values)
        async with self._write_lock:
            await self._conn.execute(
                f"UPDATE delivery_outbox SET {assignments} WHERE delivery_id = ?",
                (*values.values(), delivery_id),
            )
            await self._conn.commit()

    async def claim_delivery(self, delivery_id: str, *, updated_at: float) -> bool:
        async with self._write_lock:
            cursor = await self._conn.execute(
                """UPDATE delivery_outbox SET status = 'sending', updated_at = ?
                   WHERE delivery_id = ? AND status IN ('pending', 'retry')""",
                (updated_at, delivery_id),
            )
            await self._conn.commit()
            return cursor.rowcount == 1

    async def ensure_delivery_parts(self, delivery_id: str, operations: list[dict], *, updated_at: float) -> None:
        async with self._write_lock:
            await self._conn.executemany(
                """INSERT OR IGNORE INTO delivery_outbox_parts
                   (delivery_id, part_index, operation_json, artifact_id, status, attempts, updated_at)
                   VALUES (?, ?, ?, ?, 'pending', 0, ?)""",
                [
                    (
                        delivery_id,
                        int(operation.get("index") or 0),
                        json.dumps(operation, ensure_ascii=False),
                        str(operation.get("artifact_id") or ""),
                        updated_at,
                    )
                    for operation in operations
                ],
            )
            await self._conn.commit()

    async def delivery_part_records(self, delivery_id: str) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM delivery_outbox_parts WHERE delivery_id = ? ORDER BY part_index",
            (delivery_id,),
        )
        return [dict(row) async for row in cursor]

    async def update_delivery_part(self, delivery_id: str, part_index: int, **changes) -> None:
        allowed = {
            "status", "attempts", "platform_file_id", "message_id",
            "last_error", "ambiguous", "updated_at",
        }
        values = {key: value for key, value in changes.items() if key in allowed}
        if not values:
            return
        assignments = ", ".join(f"{key} = ?" for key in values)
        async with self._write_lock:
            await self._conn.execute(
                f"UPDATE delivery_outbox_parts SET {assignments} "
                "WHERE delivery_id = ? AND part_index = ?",
                (*values.values(), delivery_id, int(part_index)),
            )
            await self._conn.commit()

    # ── managed artifacts ─────────────────────────────

    async def insert_artifact(self, ref) -> None:
        async with self._write_lock:
            await self._conn.execute(
                """INSERT INTO artifacts
                   (artifact_id, session_key, turn_id, owner_id, kind, filename,
                    mime_type, size_bytes, content_hash, relative_path, source,
                    source_name, status, delivery_eligible, truncated, metadata_json,
                    created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ref.artifact_id, ref.session_key, ref.turn_id, ref.owner_id,
                    ref.kind, ref.filename, ref.mime_type, ref.size_bytes,
                    ref.content_hash, ref.relative_path, ref.source, ref.source_name,
                    ref.status, int(ref.delivery_eligible), int(ref.truncated),
                    json.dumps(ref.metadata or {}, ensure_ascii=False),
                    ref.created_at, ref.expires_at,
                ),
            )
            await self._conn.commit()

    async def artifact_record(self, artifact_id: str) -> dict | None:
        cursor = await self._conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?",
            (artifact_id,),
        )
        async for row in cursor:
            return dict(row)
        return None

    async def count_turn_artifacts(self, session_key: str, turn_id: str) -> int:
        cursor = await self._conn.execute(
            "SELECT COUNT(*) AS count FROM artifacts WHERE session_key = ? AND turn_id = ?",
            (session_key, turn_id),
        )
        row = await cursor.fetchone()
        return int(row["count"] if row else 0)

    async def update_artifact_status(self, artifact_id: str, status: str) -> None:
        async with self._write_lock:
            await self._conn.execute(
                "UPDATE artifacts SET status = ? WHERE artifact_id = ?",
                (status, artifact_id),
            )
            await self._conn.commit()

    async def expired_artifacts(self, now: float) -> list[dict]:
        cursor = await self._conn.execute(
            """SELECT a.* FROM artifacts AS a
               WHERE a.expires_at <= ?
                 AND NOT EXISTS (
                   SELECT 1 FROM delivery_outbox_parts AS p
                   JOIN delivery_outbox AS d ON d.delivery_id = p.delivery_id
                   WHERE p.artifact_id = a.artifact_id
                     AND d.status IN ('pending', 'retry', 'sending', 'ambiguous')
                 )
               ORDER BY a.expires_at""",
            (now,),
        )
        return [dict(row) async for row in cursor]

    async def delete_artifact(self, artifact_id: str) -> None:
        async with self._write_lock:
            await self._conn.execute("DELETE FROM artifacts WHERE artifact_id = ?", (artifact_id,))
            await self._conn.commit()

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
            await self._conn.execute(
                "DELETE FROM compression_checkpoints "
                "WHERE source_session_id = ? OR target_session_id = ?",
                (session_id, session_id),
            )
            await self._conn.execute("DELETE FROM turn_reports WHERE session_id = ?", (session_id,))
            await self._conn.execute("DELETE FROM tool_runs WHERE session_id = ?", (session_id,))
            await self._conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            await self._conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            await self._conn.commit()

    # ── compression checkpoints ──────────────────────

    async def save_compression_checkpoint(self, checkpoint: dict[str, Any]) -> int:
        row = (
            str(checkpoint.get("session_key") or ""),
            str(checkpoint.get("source_session_id") or ""),
            str(checkpoint.get("target_session_id") or ""),
            _as_int(checkpoint.get("window_number")),
            str(checkpoint.get("window_id") or ""),
            str(checkpoint.get("first_window_id") or ""),
            str(checkpoint.get("previous_window_id") or ""),
            str(checkpoint.get("trigger") or "auto"),
            str(checkpoint.get("model") or ""),
            _as_int(checkpoint.get("pre_tokens")),
            _as_int(checkpoint.get("post_tokens")),
            _as_int(checkpoint.get("summary_tokens")),
            _as_int(checkpoint.get("retained_user_tokens")),
            _as_int(checkpoint.get("pre_message_count")),
            _as_int(checkpoint.get("post_message_count")),
            json.dumps(
                clean_payload(checkpoint.get("details") or {}),
                ensure_ascii=False,
                sort_keys=True,
            ),
            _as_float(checkpoint.get("created_at")) or time.time(),
        )
        async with self._write_lock:
            cursor = await self._conn.execute(
                """INSERT INTO compression_checkpoints (
                    session_key, source_session_id, target_session_id,
                    window_number, window_id, first_window_id, previous_window_id,
                    trigger, model, pre_tokens, post_tokens, summary_tokens,
                    retained_user_tokens, pre_message_count, post_message_count,
                    details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                row,
            )
            await self._conn.commit()
        return int(cursor.lastrowid or 0)

    async def compaction_checkpoint_for_target(
        self,
        target_session_id: str,
    ) -> dict[str, Any] | None:
        rows = await self._conn.execute(
            "SELECT * FROM compression_checkpoints WHERE target_session_id = ? LIMIT 1",
            (target_session_id,),
        )
        async for row in rows:
            return _compaction_checkpoint_row(row)
        return None

    async def recent_compression_checkpoints(
        self,
        *,
        session_key: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        if session_key:
            rows = await self._conn.execute(
                """SELECT * FROM compression_checkpoints
                   WHERE session_key = ? ORDER BY id DESC LIMIT ?""",
                (session_key, int(limit)),
            )
        else:
            rows = await self._conn.execute(
                "SELECT * FROM compression_checkpoints ORDER BY id DESC LIMIT ?",
                (int(limit),),
            )
        items = [_compaction_checkpoint_row(row) async for row in rows]
        items.reverse()
        return items

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
                json.dumps(clean_payload(run.get("artifacts") or []), ensure_ascii=False, sort_keys=True),
                json.dumps(clean_payload(run.get("result_metadata") or {}), ensure_ascii=False, sort_keys=True),
                clean_text(str(run.get("error") or "")),
                str(run.get("guard_stage") or ""),
                str(run.get("reason_code") or ""),
                str(run.get("permission_category") or ""),
                str(run.get("permission_decision") or ""),
                str(run.get("required_allow") or ""),
                str(run.get("execution_mode") or ""),
                str(run.get("grant_matched") or ""),
                str(run.get("grant_scope") or ""),
                _as_float(run.get("grant_expires_at")),
                int(_as_float(run.get("temporary_grant_ttl_seconds"))),
                _as_float(run.get("created_at")) or time.time(),
            )
            for run in runs
        ]
        async with self._write_lock:
            await self._conn.executemany(
                """INSERT INTO tool_runs (
                    session_id, session_key, turn_id, tool_use_id, tool_name,
                    status, category, duration, input_summary, output_summary,
                    full_output, output_truncated, artifact_summary_json,
                    result_metadata_json, error, guard_stage, reason_code,
                    permission_category, permission_decision, required_allow,
                    execution_mode, grant_matched, grant_scope, grant_expires_at,
                    temporary_grant_ttl_seconds, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            await self._conn.commit()
        return len(rows)

    async def recent_tool_runs(
        self,
        *,
        limit: int = 20,
        session_key: str | None = None,
        turn_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if session_key:
            clauses.append("session_key = ?")
            params.append(session_key)
        if turn_id:
            clauses.append("turn_id = ?")
            params.append(turn_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
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

    # ── turn reports ──────────────────────────────────

    async def save_turn_report(self, envelope: dict[str, Any]) -> int:
        report = envelope.get("report") if isinstance(envelope.get("report"), dict) else {}
        llm = report.get("llm") if isinstance(report.get("llm"), dict) else {}
        tools = report.get("tools") if isinstance(report.get("tools"), dict) else {}
        source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
        row = (
            str(envelope.get("session_id") or ""),
            str(envelope.get("session_key") or ""),
            str(report.get("turn_id") or envelope.get("turn_id") or ""),
            str(report.get("status") or envelope.get("status") or ""),
            1 if report.get("completed") else 0,
            _as_float(report.get("duration")),
            clean_text(str(report.get("error") or "")),
            clean_text(str(report.get("user_message_summary") or "")),
            clean_text(str(report.get("final_response_summary") or "")),
            _as_int(llm.get("calls")),
            _as_int(tools.get("total")),
            _as_int(llm.get("cache_hit_tokens")),
            _as_int(llm.get("cache_miss_tokens")),
            _as_int(llm.get("cache_write_tokens")),
            _as_int(llm.get("cache_read_tokens")),
            json.dumps(clean_payload(source), ensure_ascii=False, sort_keys=True),
            json.dumps(clean_payload(report), ensure_ascii=False, sort_keys=True),
            _as_float(envelope.get("created_at")) or time.time(),
        )
        async with self._write_lock:
            cursor = await self._conn.execute(
                """INSERT INTO turn_reports (
                    session_id, session_key, turn_id, status, completed, duration,
                    error, user_message_summary, final_response_summary, llm_calls,
                    tool_calls, cache_hit_tokens, cache_miss_tokens,
                    cache_write_tokens, cache_read_tokens, source_json, report_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                row,
            )
            await self._conn.commit()
        return int(cursor.lastrowid or 0)

    async def recent_turn_reports(
        self,
        *,
        limit: int = 20,
        session_key: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if session_key:
            clauses.append("session_key = ?")
            params.append(session_key)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        rows = await self._conn.execute(
            f"""SELECT * FROM turn_reports
                {where}
                ORDER BY id DESC
                LIMIT ?""",
            params,
        )
        items: list[dict[str, Any]] = []
        async for row in rows:
            items.append(_turn_report_row(row))
        items.reverse()
        return items

    async def get_turn_report(self, report_id: int) -> dict[str, Any] | None:
        rows = await self._conn.execute(
            "SELECT * FROM turn_reports WHERE id = ?",
            (report_id,),
        )
        async for row in rows:
            return _turn_report_row(row)
        return None

    async def turn_report_summary(self) -> dict[str, Any]:
        count_rows = await self._conn.execute("SELECT COUNT(*) AS cnt FROM turn_reports")
        stored = 0
        async for row in count_rows:
            stored = int(row["cnt"] or 0)
            break
        if stored <= 0:
            return _empty_turn_report_persistence_summary()
        rows = await self._conn.execute(
            """SELECT * FROM turn_reports
               ORDER BY id DESC
               LIMIT 1"""
        )
        async for row in rows:
            item = _turn_report_row(row)
            return _turn_report_persistence_summary_from_item(item, stored=stored)
        return _empty_turn_report_persistence_summary()


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
        "artifacts": _loads_list(row["artifact_summary_json"]),
        "result_metadata": _loads_object(row["result_metadata_json"]),
        "error": str(row["error"] or ""),
        "guard_stage": str(row["guard_stage"] or ""),
        "reason_code": str(row["reason_code"] or ""),
        "permission_category": str(row["permission_category"] or ""),
        "permission_decision": str(row["permission_decision"] or ""),
        "required_allow": str(row["required_allow"] or ""),
        "execution_mode": str(row["execution_mode"] or ""),
        "grant_matched": str(row["grant_matched"] or ""),
        "grant_scope": str(row["grant_scope"] or ""),
        "grant_expires_at": float(row["grant_expires_at"] or 0.0),
        "temporary_grant_ttl_seconds": int(row["temporary_grant_ttl_seconds"] or 0),
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


def _turn_report_row(row) -> dict[str, Any]:
    source = _loads_object(row["source_json"])
    report = _loads_object(row["report_json"])
    return {
        "id": int(row["id"]),
        "session_id": str(row["session_id"] or ""),
        "session_key": str(row["session_key"] or ""),
        "turn_id": str(row["turn_id"] or ""),
        "status": str(row["status"] or ""),
        "completed": bool(row["completed"]),
        "duration": float(row["duration"] or 0.0),
        "error": str(row["error"] or ""),
        "user_message_summary": str(row["user_message_summary"] or ""),
        "final_response_summary": str(row["final_response_summary"] or ""),
        "llm_calls": int(row["llm_calls"] or 0),
        "tool_calls": int(row["tool_calls"] or 0),
        "cache_hit_tokens": int(row["cache_hit_tokens"] or 0),
        "cache_miss_tokens": int(row["cache_miss_tokens"] or 0),
        "cache_write_tokens": int(row["cache_write_tokens"] or 0),
        "cache_read_tokens": int(row["cache_read_tokens"] or 0),
        "source": source,
        "report": report,
        "created_at": float(row["created_at"] or 0.0),
    }


def _compaction_checkpoint_row(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "session_key": str(row["session_key"] or ""),
        "source_session_id": str(row["source_session_id"] or ""),
        "target_session_id": str(row["target_session_id"] or ""),
        "window_number": int(row["window_number"] or 0),
        "window_id": str(row["window_id"] or ""),
        "first_window_id": str(row["first_window_id"] or ""),
        "previous_window_id": str(row["previous_window_id"] or ""),
        "trigger": str(row["trigger"] or ""),
        "model": str(row["model"] or ""),
        "pre_tokens": int(row["pre_tokens"] or 0),
        "post_tokens": int(row["post_tokens"] or 0),
        "summary_tokens": int(row["summary_tokens"] or 0),
        "retained_user_tokens": int(row["retained_user_tokens"] or 0),
        "pre_message_count": int(row["pre_message_count"] or 0),
        "post_message_count": int(row["post_message_count"] or 0),
        "details": _loads_object(row["details_json"]),
        "created_at": float(row["created_at"] or 0.0),
    }


def _loads_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _loads_list(value: Any) -> list[Any]:
    if not value:
        return []
    try:
        decoded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return []
    return decoded if isinstance(decoded, list) else []


def _empty_turn_report_persistence_summary() -> dict[str, Any]:
    return {
        "stored": 0,
        "last_id": 0,
        "last_turn_id": "",
        "last_session_key": "",
        "last_status": "",
        "last_error": "",
        "last_duration": 0.0,
        "last_llm_calls": 0,
        "last_tool_calls": 0,
        "last_cache_hit_tokens": 0,
        "last_cache_miss_tokens": 0,
        "last_cache_write_tokens": 0,
        "last_cache_read_tokens": 0,
    }


def _turn_report_persistence_summary_from_item(
    item: dict[str, Any],
    *,
    stored: int,
) -> dict[str, Any]:
    return {
        "stored": int(stored),
        "last_id": int(item.get("id") or 0),
        "last_turn_id": str(item.get("turn_id") or ""),
        "last_session_key": str(item.get("session_key") or ""),
        "last_status": str(item.get("status") or ""),
        "last_error": str(item.get("error") or ""),
        "last_duration": float(item.get("duration") or 0.0),
        "last_llm_calls": int(item.get("llm_calls") or 0),
        "last_tool_calls": int(item.get("tool_calls") or 0),
        "last_cache_hit_tokens": int(item.get("cache_hit_tokens") or 0),
        "last_cache_miss_tokens": int(item.get("cache_miss_tokens") or 0),
        "last_cache_write_tokens": int(item.get("cache_write_tokens") or 0),
        "last_cache_read_tokens": int(item.get("cache_read_tokens") or 0),
    }


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
