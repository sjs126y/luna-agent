"""SQLite source of truth for memory observations, history, and review state."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from personal_agent.memory.models import MemoryRecord, MemoryScope, Observation, ObservationKind, utc_now

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_schema (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS review_checkpoints (
  scope_key TEXT PRIMARY KEY, last_turn_id TEXT DEFAULT '', reviewed_turns INTEGER DEFAULT 0, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_batches (
  id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, from_turn_id TEXT DEFAULT '', to_turn_id TEXT DEFAULT '',
  status TEXT NOT NULL, requested_provider TEXT DEFAULT '', effective_provider TEXT DEFAULT '',
  error TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
  id TEXT PRIMARY KEY, batch_id TEXT DEFAULT '', scope_key TEXT NOT NULL, kind TEXT NOT NULL,
  content TEXT NOT NULL, content_hash TEXT NOT NULL, importance REAL NOT NULL, long_term INTEGER NOT NULL,
  source_turn_ids_json TEXT NOT NULL, migration_status TEXT DEFAULT '', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observations_scope ON observations(scope_key, created_at);
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
  content_hash TEXT NOT NULL, importance REAL NOT NULL, provider TEXT NOT NULL,
  index_status TEXT DEFAULT 'ready', metadata_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope_key, updated_at);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(memory_id UNINDEXED, scope_key UNINDEXED, content);
CREATE TABLE IF NOT EXISTS memory_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL, action TEXT NOT NULL,
  previous_content TEXT DEFAULT '', content TEXT DEFAULT '', provider TEXT DEFAULT '', reason TEXT DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS internal_buffer (
  observation_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, content_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', target_file TEXT DEFAULT '', reason TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_internal_buffer_dedupe ON internal_buffer(scope_key, content_hash);
CREATE TABLE IF NOT EXISTS internal_revisions (
  profile TEXT PRIMARY KEY, revision INTEGER NOT NULL, file_hashes_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_state (
  scope_key TEXT PRIMARY KEY, requested_provider TEXT DEFAULT '', effective_provider TEXT DEFAULT '',
  fallback_reason TEXT DEFAULT '', state_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


class MemoryArchive:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        row = await self._fetchone("SELECT version FROM memory_schema LIMIT 1")
        if row is None:
            await self._conn.execute("INSERT INTO memory_schema(version) VALUES (?)", (SCHEMA_VERSION,))
        elif int(row["version"]) != SCHEMA_VERSION:
            raise RuntimeError(f"Unsupported memory schema version: {row['version']}")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def create_review_batch(self, scope: MemoryScope, *, requested: str, effective: str,
                                  from_turn_id: str = "", to_turn_id: str = "") -> str:
        batch_id = uuid4().hex
        now = utc_now()
        await self._execute_write(
            "INSERT INTO review_batches VALUES (?, ?, ?, ?, 'pending', ?, ?, '', ?, ?)",
            (batch_id, _scope_key(scope), from_turn_id, to_turn_id, requested, effective, now, now),
        )
        return batch_id

    async def finish_review_batch(self, batch_id: str, *, status: str, error: str = "") -> None:
        await self._execute_write(
            "UPDATE review_batches SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, error, utc_now(), batch_id),
        )

    async def save_observations(self, scope: MemoryScope, observations: tuple[Observation, ...],
                                *, batch_id: str = "", migration_status: str = "") -> None:
        if not observations:
            return
        async with self._write_lock:
            for item in observations:
                await self._connection.execute(
                    """INSERT OR IGNORE INTO observations
                    (id,batch_id,scope_key,kind,content,content_hash,importance,long_term,source_turn_ids_json,migration_status,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (item.id, batch_id, _scope_key(scope), item.kind.value, item.content,
                     _content_hash(item.content), item.importance, int(item.long_term),
                     json.dumps(item.source_turn_ids), migration_status, item.created_at),
                )
            await self._connection.commit()

    async def upsert_memory(self, scope: MemoryScope, record: MemoryRecord, *, action: str = "ADD",
                            previous_content: str = "", reason: str = "") -> None:
        now = utc_now()
        async with self._write_lock:
            await self._connection.execute(
                """INSERT INTO memories
                (id,scope_key,kind,content,content_hash,importance,provider,index_status,metadata_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,content=excluded.content,
                content_hash=excluded.content_hash,importance=excluded.importance,provider=excluded.provider,
                index_status=excluded.index_status,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
                (record.id, _scope_key(scope), record.kind.value, record.content, _content_hash(record.content),
                 record.importance, record.provider, record.metadata.get("index_status", "ready"),
                 json.dumps(record.metadata, ensure_ascii=False), record.created_at, now),
            )
            await self._connection.execute("DELETE FROM memories_fts WHERE memory_id = ?", (record.id,))
            await self._connection.execute(
                "INSERT INTO memories_fts(memory_id,scope_key,content) VALUES (?,?,?)",
                (record.id, _scope_key(scope), record.content),
            )
            await self._connection.execute(
                "INSERT INTO memory_history(memory_id,action,previous_content,content,provider,reason,created_at) VALUES (?,?,?,?,?,?,?)",
                (record.id, action, previous_content, record.content, record.provider, reason, now),
            )
            await self._connection.commit()

    async def list_memories(self, scope: MemoryScope, *, limit: int = 100) -> list[MemoryRecord]:
        rows = await self._fetchall(
            "SELECT * FROM memories WHERE scope_key = ? ORDER BY updated_at DESC LIMIT ?",
            (_scope_key(scope), limit),
        )
        return [_record_from_row(row, scope) for row in rows]

    async def search_bm25(self, scope: MemoryScope, query: str, *, limit: int = 10) -> list[MemoryRecord]:
        if not query.strip():
            return []
        rows = await self._fetchall(
            """SELECT m.*, bm25(memories_fts) AS rank FROM memories_fts
            JOIN memories m ON m.id = memories_fts.memory_id
            WHERE memories_fts MATCH ? AND memories_fts.scope_key = ? ORDER BY rank LIMIT ?""",
            (_fts_query(query), _scope_key(scope), limit),
        )
        return [MemoryRecord(**{**_record_from_row(row, scope).__dict__, "score": 1 / (1 + abs(float(row["rank"])))}) for row in rows]

    async def add_to_internal_buffer(self, scope: MemoryScope, observations: tuple[Observation, ...]) -> int:
        now = utc_now()
        inserted = 0
        async with self._write_lock:
            for item in observations:
                cursor = await self._connection.execute(
                    "INSERT OR IGNORE INTO internal_buffer(observation_id,scope_key,content_hash,status,created_at,updated_at) VALUES (?,?,?,'pending',?,?)",
                    (item.id, _scope_key(scope), _content_hash(item.content), now, now),
                )
                inserted += max(cursor.rowcount, 0)
            await self._connection.commit()
        return inserted

    async def pending_buffer_count(self, scope: MemoryScope) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS count FROM internal_buffer WHERE scope_key = ? AND status = 'pending'",
            (_scope_key(scope),),
        )
        return int(row["count"]) if row else 0

    async def pending_buffer_observations(self, scope: MemoryScope, *, limit: int = 100) -> list[Observation]:
        rows = await self._fetchall(
            """SELECT o.* FROM internal_buffer b JOIN observations o ON o.id = b.observation_id
            WHERE b.scope_key = ? AND b.status = 'pending' ORDER BY b.created_at LIMIT ?""",
            (_scope_key(scope), limit),
        )
        return [Observation(
            id=row["id"], kind=ObservationKind(row["kind"]), content=row["content"],
            importance=float(row["importance"]), long_term=bool(row["long_term"]),
            source_turn_ids=tuple(json.loads(row["source_turn_ids_json"] or "[]")),
            created_at=row["created_at"],
        ) for row in rows]

    async def set_buffer_status(self, observation_id: str, status: str, *, target_file: str = "", reason: str = "") -> None:
        if status not in {"pending", "applied", "skipped", "conflict"}:
            raise ValueError(f"Invalid internal buffer status: {status}")
        await self._execute_write(
            "UPDATE internal_buffer SET status = ?, target_file = ?, reason = ?, updated_at = ? WHERE observation_id = ?",
            (status, target_file, reason, utc_now(), observation_id),
        )

    async def list_buffer(self, scope: MemoryScope, *, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            """SELECT b.*, o.kind, o.content, o.importance FROM internal_buffer b
            JOIN observations o ON o.id = b.observation_id
            WHERE b.scope_key = ? AND b.status = ? ORDER BY b.created_at LIMIT ?""",
            (_scope_key(scope), status, limit),
        )
        return [dict(row) for row in rows]

    async def set_checkpoint(self, scope: MemoryScope, *, last_turn_id: str, reviewed_turns: int) -> None:
        await self._execute_write(
            """INSERT INTO review_checkpoints(scope_key,last_turn_id,reviewed_turns,updated_at) VALUES (?,?,?,?)
            ON CONFLICT(scope_key) DO UPDATE SET last_turn_id=excluded.last_turn_id,
            reviewed_turns=excluded.reviewed_turns,updated_at=excluded.updated_at""",
            (_scope_key(scope), last_turn_id, reviewed_turns, utc_now()),
        )

    async def get_checkpoint(self, scope: MemoryScope) -> dict[str, Any] | None:
        row = await self._fetchone("SELECT * FROM review_checkpoints WHERE scope_key = ?", (_scope_key(scope),))
        return dict(row) if row else None

    async def next_internal_revision(self, profile: str, file_hashes: dict[str, str]) -> int:
        row = await self._fetchone("SELECT revision FROM internal_revisions WHERE profile = ?", (profile,))
        revision = (int(row["revision"]) if row else 0) + 1
        await self._execute_write(
            """INSERT INTO internal_revisions(profile,revision,file_hashes_json,created_at) VALUES (?,?,?,?)
            ON CONFLICT(profile) DO UPDATE SET revision=excluded.revision,file_hashes_json=excluded.file_hashes_json,created_at=excluded.created_at""",
            (profile, revision, json.dumps(file_hashes, sort_keys=True), utc_now()),
        )
        return revision

    async def _execute_write(self, sql: str, params: tuple[Any, ...]) -> None:
        async with self._write_lock:
            await self._connection.execute(sql, params)
            await self._connection.commit()

    async def _fetchone(self, sql: str, params: tuple[Any, ...] = ()):
        cursor = await self._connection.execute(sql, params)
        return await cursor.fetchone()

    async def _fetchall(self, sql: str, params: tuple[Any, ...] = ()):
        cursor = await self._connection.execute(sql, params)
        return await cursor.fetchall()

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("MemoryArchive is not initialized")
        return self._conn


def _scope_key(scope: MemoryScope) -> str:
    return f"{scope.agent_id}:{scope.user_id}:{scope.profile}"


def _content_hash(content: str) -> str:
    normalized = " ".join(content.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _fts_query(query: str) -> str:
    terms = [part.replace('"', '""') for part in query.split() if part]
    return " OR ".join(f'"{term}"' for term in terms) or '""'


def _record_from_row(row, scope: MemoryScope) -> MemoryRecord:
    return MemoryRecord(
        id=row["id"], content=row["content"], kind=ObservationKind(row["kind"]),
        importance=float(row["importance"]), provider=row["provider"], scope=scope,
        created_at=row["created_at"], updated_at=row["updated_at"],
        metadata=json.loads(row["metadata_json"] or "{}"),
    )
