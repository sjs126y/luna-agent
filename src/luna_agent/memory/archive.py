"""SQLite source of truth for memory observations, history, and review state."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from luna_agent.memory.models import MemoryRecord, MemoryScope, Observation, ObservationKind, utc_now

SCHEMA_VERSION = 5
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
  source_turn_ids_json TEXT NOT NULL, migration_status TEXT DEFAULT '', migration_attempts INTEGER DEFAULT 0,
  migration_error TEXT DEFAULT '', migration_updated_at TEXT DEFAULT '', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observations_scope ON observations(scope_key, created_at);
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
  content_hash TEXT NOT NULL, importance REAL NOT NULL, provider TEXT NOT NULL,
  index_status TEXT DEFAULT 'ready', index_attempts INTEGER DEFAULT 0,
  index_error TEXT DEFAULT '', index_updated_at TEXT DEFAULT '',
  metadata_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope_key, updated_at);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(memory_id UNINDEXED, scope_key UNINDEXED, content);
CREATE TABLE IF NOT EXISTS memory_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL, action TEXT NOT NULL,
  previous_content TEXT DEFAULT '', content TEXT DEFAULT '', provider TEXT DEFAULT '', reason TEXT DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_index_backends (
  index_kind TEXT PRIMARY KEY, backend TEXT NOT NULL, fingerprint TEXT NOT NULL,
  generation TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_index_state (
  memory_id TEXT NOT NULL, index_kind TEXT NOT NULL, backend TEXT NOT NULL,
  fingerprint TEXT NOT NULL, generation TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER DEFAULT 0, error TEXT DEFAULT '', updated_at TEXT NOT NULL,
  PRIMARY KEY(memory_id, index_kind)
);
CREATE INDEX IF NOT EXISTS idx_memory_index_state_status
ON memory_index_state(index_kind, status, attempts, updated_at);
CREATE TABLE IF NOT EXISTS internal_buffer (
  observation_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, content_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', target_file TEXT DEFAULT '', reason TEXT DEFAULT '',
  proposed_action TEXT DEFAULT '', proposed_content TEXT DEFAULT '', entry_id TEXT DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
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
        elif int(row["version"]) < SCHEMA_VERSION:
            await self._migrate(int(row["version"]))
        elif int(row["version"]) > SCHEMA_VERSION:
            raise RuntimeError(f"Unsupported memory schema version: {row['version']}")
        await self._conn.commit()

    async def _migrate(self, version: int) -> None:
        if version == 1:
            cursor = await self._connection.execute("PRAGMA table_info(internal_buffer)")
            columns = {row["name"] for row in await cursor.fetchall()}
            for name in ("proposed_action", "proposed_content", "entry_id"):
                if name not in columns:
                    await self._connection.execute(
                        f"ALTER TABLE internal_buffer ADD COLUMN {name} TEXT DEFAULT ''"
                    )
            await self._connection.execute("UPDATE memory_schema SET version = 2")
            version = 2
        if version == 2:
            cursor = await self._connection.execute("PRAGMA table_info(observations)")
            columns = {row["name"] for row in await cursor.fetchall()}
            additions = {
                "migration_attempts": "INTEGER DEFAULT 0",
                "migration_error": "TEXT DEFAULT ''",
                "migration_updated_at": "TEXT DEFAULT ''",
            }
            for name, definition in additions.items():
                if name not in columns:
                    await self._connection.execute(
                        f"ALTER TABLE observations ADD COLUMN {name} {definition}"
                    )
            await self._connection.execute("UPDATE memory_schema SET version = 3")
            version = 3
        if version == 3:
            cursor = await self._connection.execute("PRAGMA table_info(memories)")
            columns = {row["name"] for row in await cursor.fetchall()}
            additions = {
                "index_attempts": "INTEGER DEFAULT 0",
                "index_error": "TEXT DEFAULT ''",
                "index_updated_at": "TEXT DEFAULT ''",
            }
            for name, definition in additions.items():
                if name not in columns:
                    await self._connection.execute(
                        f"ALTER TABLE memories ADD COLUMN {name} {definition}"
                    )
            await self._connection.execute("UPDATE memory_schema SET version = 4")
            version = 4
        if version == 4:
            await self._connection.execute("UPDATE memory_schema SET version = 5")
            version = 5
        if version != SCHEMA_VERSION:
            raise RuntimeError(f"Unsupported memory schema version: {version}")

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

    async def all_memories(self, *, limit: int = 100000) -> list[MemoryRecord]:
        rows = await self._fetchall(
            "SELECT * FROM memories ORDER BY updated_at LIMIT ?", (limit,)
        )
        result: list[MemoryRecord] = []
        for row in rows:
            agent_id, user_id, profile = str(row["scope_key"]).split(":", 2)
            result.append(_record_from_row(
                row,
                MemoryScope(user_id=user_id, agent_id=agent_id, profile=profile),
            ))
        return result

    async def scope_keys(self) -> list[str]:
        """Return all persisted memory scope keys for explicit migrations."""
        rows = await self._fetchall(
            """SELECT scope_key FROM observations
               UNION SELECT scope_key FROM memories
               UNION SELECT scope_key FROM review_checkpoints
               UNION SELECT scope_key FROM review_batches
               UNION SELECT scope_key FROM internal_buffer
               UNION SELECT scope_key FROM provider_state
               ORDER BY scope_key"""
        )
        return [str(row["scope_key"] or "") for row in rows if str(row["scope_key"] or "")]

    async def migrate_scope_keys(
        self,
        source_scopes: list[str] | tuple[str, ...],
        *,
        target_user_id: str = "owner",
        apply: bool = False,
    ) -> dict[str, Any]:
        """Plan or apply a conservative owner scope migration.

        The migration is deliberately archive-local. External providers can
        rebuild from this source of truth after the transaction commits.
        """
        sources = tuple(dict.fromkeys(str(item or "").strip() for item in source_scopes if str(item or "").strip()))
        plan = {
            "source_scopes": list(sources),
            "target_user_id": str(target_user_id or "owner"),
            "apply": bool(apply),
            "moved": 0,
            "merged": 0,
            "conflicts": [],
        }
        if not sources:
            plan["available_scopes"] = await self.scope_keys()
            return plan
        if not apply:
            plan["targets"] = [_target_scope_key(item, plan["target_user_id"]) for item in sources]
            return plan

        async with self._write_lock:
            await self._connection.execute("BEGIN")
            try:
                for source in sources:
                    target = _target_scope_key(source, plan["target_user_id"])
                    if target == source:
                        continue
                    plan["moved"] += await self._migrate_scope(source, target, plan)
                await self._connection.commit()
            except Exception:
                await self._connection.rollback()
                raise
        return plan

    async def _migrate_scope(self, source: str, target: str, plan: dict[str, Any]) -> int:
        moved = 0
        # Memory records are the only scope-bearing rows with a stable ID and
        # external indexes. Merge duplicate content before changing scope.
        source_rows = await self._fetchall(
            "SELECT id, content_hash FROM memories WHERE scope_key = ?", (source,)
        )
        target_hashes = {
            str(row["content_hash"]): str(row["id"])
            for row in await self._fetchall(
                "SELECT id, content_hash FROM memories WHERE scope_key = ?", (target,)
            )
        }
        for row in source_rows:
            memory_id = str(row["id"])
            content_hash = str(row["content_hash"])
            existing_id = target_hashes.get(content_hash)
            if existing_id and existing_id != memory_id:
                await self._connection.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
                await self._connection.execute("DELETE FROM memory_index_state WHERE memory_id = ?", (memory_id,))
                await self._connection.execute("UPDATE memory_history SET memory_id = ? WHERE memory_id = ?", (existing_id, memory_id))
                await self._connection.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
                plan["merged"] += 1
                plan["conflicts"].append({"source_id": memory_id, "target_id": existing_id, "kind": "duplicate_content"})
            else:
                target_hashes[content_hash] = memory_id
                moved += 1

        # Avoid the unique (scope_key, content_hash) constraint in the buffer.
        await self._connection.execute(
            """DELETE FROM internal_buffer WHERE scope_key = ? AND content_hash IN (
                SELECT content_hash FROM internal_buffer WHERE scope_key = ?
            )""",
            (source, target),
        )
        await self._connection.execute("UPDATE observations SET scope_key = ? WHERE scope_key = ?", (target, source))
        await self._connection.execute("UPDATE review_batches SET scope_key = ? WHERE scope_key = ?", (target, source))
        await self._connection.execute("UPDATE memories SET scope_key = ?, index_status = 'pending' WHERE scope_key = ?", (target, source))
        await self._connection.execute("UPDATE memories_fts SET scope_key = ? WHERE scope_key = ?", (target, source))
        await self._connection.execute("UPDATE internal_buffer SET scope_key = ? WHERE scope_key = ?", (target, source))

        checkpoint = await self._fetchone("SELECT * FROM review_checkpoints WHERE scope_key = ?", (source,))
        if checkpoint:
            target_checkpoint = await self._fetchone("SELECT * FROM review_checkpoints WHERE scope_key = ?", (target,))
            if target_checkpoint:
                reviewed = max(int(checkpoint["reviewed_turns"] or 0), int(target_checkpoint["reviewed_turns"] or 0))
                chosen_turn = str(checkpoint["last_turn_id"] or target_checkpoint["last_turn_id"] or "")
                await self._connection.execute(
                    "UPDATE review_checkpoints SET last_turn_id = ?, reviewed_turns = ?, updated_at = ? WHERE scope_key = ?",
                    (chosen_turn, reviewed, max(str(checkpoint["updated_at"] or ""), str(target_checkpoint["updated_at"] or "")), target),
                )
                await self._connection.execute("DELETE FROM review_checkpoints WHERE scope_key = ?", (source,))
            else:
                await self._connection.execute("UPDATE review_checkpoints SET scope_key = ? WHERE scope_key = ?", (target, source))
        provider_state = await self._fetchone("SELECT * FROM provider_state WHERE scope_key = ?", (source,))
        if provider_state:
            target_state = await self._fetchone("SELECT * FROM provider_state WHERE scope_key = ?", (target,))
            if target_state:
                await self._connection.execute("DELETE FROM provider_state WHERE scope_key = ?", (source,))
            else:
                await self._connection.execute("UPDATE provider_state SET scope_key = ? WHERE scope_key = ?", (target, source))
        return moved

    async def get_memory(self, memory_id: str, scope: MemoryScope) -> MemoryRecord | None:
        row = await self._fetchone(
            "SELECT * FROM memories WHERE id = ? AND scope_key = ?", (memory_id, _scope_key(scope))
        )
        return _record_from_row(row, scope) if row else None

    async def get_memories(self, memory_ids: list[str], scope: MemoryScope) -> list[MemoryRecord]:
        if not memory_ids:
            return []
        placeholders = ",".join("?" for _ in memory_ids)
        rows = await self._fetchall(
            f"SELECT * FROM memories WHERE scope_key = ? AND id IN ({placeholders})",
            (_scope_key(scope), *memory_ids),
        )
        by_id = {row["id"]: _record_from_row(row, scope) for row in rows}
        return [by_id[item] for item in memory_ids if item in by_id]

    async def find_memory_by_content(self, scope: MemoryScope, content: str) -> MemoryRecord | None:
        row = await self._fetchone(
            "SELECT * FROM memories WHERE scope_key = ? AND content_hash = ? LIMIT 1",
            (_scope_key(scope), _content_hash(content)),
        )
        return _record_from_row(row, scope) if row else None

    async def set_memory_index_status(self, memory_id: str, status: str) -> None:
        if status == "ready":
            await self.mark_memory_index_ready(memory_id)
            return
        await self._execute_write(
            """UPDATE memories SET index_status = ?, index_updated_at = ?,
            updated_at = ? WHERE id = ?""",
            (status, utc_now(), utc_now(), memory_id),
        )

    async def mark_memory_index_ready(self, memory_id: str) -> None:
        now = utc_now()
        await self._execute_write(
            """UPDATE memories SET index_status = 'ready', index_attempts = index_attempts + 1,
            index_error = '', index_updated_at = ?, updated_at = ? WHERE id = ?""",
            (now, now, memory_id),
        )

    async def mark_memory_index_failed(self, memory_id: str, error: str) -> None:
        now = utc_now()
        await self._execute_write(
            """UPDATE memories SET index_status = 'pending', index_attempts = index_attempts + 1,
            index_error = ?, index_updated_at = ?, updated_at = ? WHERE id = ?""",
            (error, now, now, memory_id),
        )

    async def pending_index_memories(self, scope: MemoryScope, *, limit: int = 10) -> list[MemoryRecord]:
        rows = await self._fetchall(
            """SELECT m.* FROM memories m
            LEFT JOIN memory_index_state s ON s.memory_id = m.id AND s.index_kind = 'vector'
            WHERE m.scope_key = ? AND COALESCE(s.status, m.index_status) = 'pending'
            ORDER BY COALESCE(s.attempts, m.index_attempts), m.updated_at LIMIT ?""",
            (_scope_key(scope), limit),
        )
        return [_record_from_row(row, scope) for row in rows]

    async def ensure_index_backend(
        self,
        index_kind: str,
        backend: str,
        fingerprint: str,
        *,
        initial_status: str = "pending",
    ) -> bool:
        if index_kind not in {"vector", "keyword"}:
            raise ValueError(f"Unsupported memory index kind: {index_kind}")
        generation = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
        now = utc_now()
        async with self._write_lock:
            cursor = await self._connection.execute(
                "SELECT * FROM memory_index_backends WHERE index_kind = ?", (index_kind,)
            )
            current = await cursor.fetchone()
            changed = current is not None and (
                current["backend"] != backend or current["fingerprint"] != fingerprint
            )
            if current is None or changed:
                await self._connection.execute(
                    """INSERT INTO memory_index_backends(index_kind,backend,fingerprint,generation,updated_at)
                    VALUES (?,?,?,?,?) ON CONFLICT(index_kind) DO UPDATE SET
                    backend=excluded.backend,fingerprint=excluded.fingerprint,
                    generation=excluded.generation,updated_at=excluded.updated_at""",
                    (index_kind, backend, fingerprint, generation, now),
                )
                status_expression = "?" if changed or index_kind == "keyword" else "index_status"
                status_params: tuple[Any, ...] = (initial_status,) if status_expression == "?" else ()
                await self._connection.execute(
                    f"""INSERT INTO memory_index_state
                    (memory_id,index_kind,backend,fingerprint,generation,status,attempts,error,updated_at)
                    SELECT id,?,?,?,?,{status_expression},0,'',? FROM memories WHERE true
                    ON CONFLICT(memory_id,index_kind) DO UPDATE SET
                    backend=excluded.backend,fingerprint=excluded.fingerprint,
                    generation=excluded.generation,status=excluded.status,attempts=0,error='',
                    updated_at=excluded.updated_at""",
                    (index_kind, backend, fingerprint, generation, *status_params, now),
                )
                await self._connection.commit()
            return changed

    async def set_backend_index_status(
        self,
        memory_id: str,
        index_kind: str,
        *,
        backend: str,
        fingerprint: str,
        status: str,
        error: str = "",
    ) -> None:
        generation = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
        await self._execute_write(
            """INSERT INTO memory_index_state
            (memory_id,index_kind,backend,fingerprint,generation,status,attempts,error,updated_at)
            VALUES (?,?,?,?,?,?,1,?,?) ON CONFLICT(memory_id,index_kind) DO UPDATE SET
            backend=excluded.backend,fingerprint=excluded.fingerprint,generation=excluded.generation,
            status=excluded.status,attempts=memory_index_state.attempts + 1,
            error=excluded.error,updated_at=excluded.updated_at""",
            (memory_id, index_kind, backend, fingerprint, generation, status, error, utc_now()),
        )

    async def pending_backend_index_memories(
        self,
        scope: MemoryScope,
        *,
        limit: int = 10,
    ) -> list[MemoryRecord]:
        rows = await self._fetchall(
            """SELECT DISTINCT m.* FROM memories m JOIN memory_index_state s ON s.memory_id = m.id
            WHERE m.scope_key = ? AND s.status = 'pending'
            ORDER BY s.attempts, s.updated_at LIMIT ?""",
            (_scope_key(scope), limit),
        )
        return [_record_from_row(row, scope) for row in rows]

    async def backend_index_status(self, scope: MemoryScope | None = None) -> dict[str, dict[str, int]]:
        where = "WHERE m.scope_key = ?" if scope is not None else ""
        params = (_scope_key(scope),) if scope is not None else ()
        rows = await self._fetchall(
            f"""SELECT s.index_kind,s.status,COUNT(*) AS count FROM memory_index_state s
            JOIN memories m ON m.id = s.memory_id {where}
            GROUP BY s.index_kind,s.status""",
            params,
        )
        result: dict[str, dict[str, int]] = {}
        for row in rows:
            result.setdefault(str(row["index_kind"]), {})[str(row["status"])] = int(row["count"])
        return result

    async def index_backend_metadata(self) -> dict[str, dict[str, str]]:
        rows = await self._fetchall("SELECT * FROM memory_index_backends ORDER BY index_kind")
        return {str(row["index_kind"]): dict(row) for row in rows}

    async def index_status_counts(self, scope: MemoryScope | None = None) -> dict[str, int]:
        where = "WHERE scope_key = ?" if scope is not None else ""
        params = (_scope_key(scope),) if scope is not None else ()
        rows = await self._fetchall(
            f"""SELECT COALESCE(NULLIF(index_status, ''), 'unknown') AS status,
            COUNT(*) AS count FROM memories {where} GROUP BY status""",
            params,
        )
        return {str(row["status"]): int(row["count"]) for row in rows}

    async def delete_memory(self, memory_id: str, scope: MemoryScope, *, provider: str = "", reason: str = "") -> bool:
        existing = await self.get_memory(memory_id, scope)
        if existing is None:
            return False
        async with self._write_lock:
            await self._connection.execute("DELETE FROM memories WHERE id = ? AND scope_key = ?", (memory_id, _scope_key(scope)))
            await self._connection.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
            await self._connection.execute("DELETE FROM memory_index_state WHERE memory_id = ?", (memory_id,))
            await self._connection.execute(
                "INSERT INTO memory_history(memory_id,action,previous_content,content,provider,reason,created_at) VALUES (?,'DELETE',?,'',?,?,?)",
                (memory_id, existing.content, provider, reason, utc_now()),
            )
            await self._connection.commit()
        return True

    async def memory_history(self, memory_id: str) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            "SELECT * FROM memory_history WHERE memory_id = ? ORDER BY id", (memory_id,)
        )
        return [dict(row) for row in rows]

    async def pending_migration_observations(self, scope: MemoryScope, *, limit: int = 100) -> list[Observation]:
        rows = await self._fetchall(
            """SELECT * FROM observations WHERE scope_key = ? AND migration_status = 'pending'
            ORDER BY migration_attempts, created_at LIMIT ?""",
            (_scope_key(scope), limit),
        )
        return [Observation(
            id=row["id"], kind=ObservationKind(row["kind"]), content=row["content"],
            importance=float(row["importance"]), long_term=bool(row["long_term"]),
            source_turn_ids=tuple(json.loads(row["source_turn_ids_json"] or "[]")),
            created_at=row["created_at"],
        ) for row in rows]

    async def mark_observations_migrated(self, observation_ids: list[str]) -> None:
        if not observation_ids:
            return
        placeholders = ",".join("?" for _ in observation_ids)
        async with self._write_lock:
            await self._connection.execute(
                f"""UPDATE observations SET migration_status = 'migrated',
                migration_attempts = migration_attempts + 1, migration_error = '',
                migration_updated_at = ? WHERE id IN ({placeholders})""",
                (utc_now(), *observation_ids),
            )
            await self._connection.commit()

    async def mark_observation_migration_failed(self, observation_id: str, error: str) -> None:
        await self._execute_write(
            """UPDATE observations SET migration_status = 'pending',
            migration_attempts = migration_attempts + 1, migration_error = ?,
            migration_updated_at = ? WHERE id = ?""",
            (error, utc_now(), observation_id),
        )

    async def migration_status_counts(self, scope: MemoryScope | None = None) -> dict[str, int]:
        where = "WHERE scope_key = ?" if scope is not None else ""
        params = (_scope_key(scope),) if scope is not None else ()
        rows = await self._fetchall(
            f"""SELECT COALESCE(NULLIF(migration_status, ''), 'legacy_empty') AS status,
            COUNT(*) AS count FROM observations {where} GROUP BY status""",
            params,
        )
        return {str(row["status"]): int(row["count"]) for row in rows}

    async def set_provider_state(self, scope: MemoryScope, *, requested: str, effective: str,
                                 fallback_reason: str = "", state: dict[str, Any] | None = None) -> None:
        await self._execute_write(
            """INSERT INTO provider_state(scope_key,requested_provider,effective_provider,fallback_reason,state_json,updated_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET requested_provider=excluded.requested_provider,
            effective_provider=excluded.effective_provider,fallback_reason=excluded.fallback_reason,
            state_json=excluded.state_json,updated_at=excluded.updated_at""",
            (_scope_key(scope), requested, effective, fallback_reason,
             json.dumps(state or {}, ensure_ascii=False), utc_now()),
        )

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

    async def set_buffer_status(
        self,
        observation_id: str,
        status: str,
        *,
        target_file: str = "",
        reason: str = "",
        proposed_action: str = "",
        proposed_content: str = "",
        entry_id: str = "",
    ) -> None:
        if status not in {"pending", "applied", "skipped", "conflict"}:
            raise ValueError(f"Invalid internal buffer status: {status}")
        await self._execute_write(
            """UPDATE internal_buffer SET status = ?, target_file = ?, reason = ?,
            proposed_action = ?, proposed_content = ?, entry_id = ?, updated_at = ?
            WHERE observation_id = ?""",
            (status, target_file, reason, proposed_action, proposed_content, entry_id,
             utc_now(), observation_id),
        )

    async def list_buffer(self, scope: MemoryScope, *, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            """SELECT b.*, o.kind, o.content, o.importance FROM internal_buffer b
            JOIN observations o ON o.id = b.observation_id
            WHERE b.scope_key = ? AND b.status = ? ORDER BY b.created_at LIMIT ?""",
            (_scope_key(scope), status, limit),
        )
        return [dict(row) for row in rows]

    async def get_buffer_item(self, scope: MemoryScope, observation_id: str) -> dict[str, Any] | None:
        row = await self._fetchone(
            """SELECT b.*, o.kind, o.content, o.importance FROM internal_buffer b
            JOIN observations o ON o.id = b.observation_id
            WHERE b.scope_key = ? AND b.observation_id = ?""",
            (_scope_key(scope), observation_id),
        )
        return dict(row) if row else None

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


def _target_scope_key(source: str, user_id: str) -> str:
    parts = str(source or "").split(":", 2)
    if len(parts) != 3:
        raise ValueError(f"Invalid memory scope key: {source}")
    return f"{parts[0]}:{str(user_id or 'owner').strip() or 'owner'}:{parts[2]}"


def _content_hash(content: str) -> str:
    normalized = " ".join(content.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _fts_query(query: str) -> str:
    terms = [part.replace('"', '""') for part in query.split() if part]
    return " OR ".join(f'"{term}"' for term in terms) or '""'


def _record_from_row(row, scope: MemoryScope) -> MemoryRecord:
    metadata = json.loads(row["metadata_json"] or "{}")
    metadata["index_status"] = row["index_status"]
    return MemoryRecord(
        id=row["id"], content=row["content"], kind=ObservationKind(row["kind"]),
        importance=float(row["importance"]), provider=row["provider"], scope=scope,
        created_at=row["created_at"], updated_at=row["updated_at"],
        metadata=metadata,
    )
