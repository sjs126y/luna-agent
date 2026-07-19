from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from pathlib import Path
from typing import Any

from luna_agent.artifacts.models import ArtifactSource, ArtifactStatus, StoredArtifactRef


class ArtifactStoreError(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail or reason)


class ArtifactStore:
    def __init__(
        self,
        root: Path,
        db,
        *,
        max_file_bytes: int = 20 * 1024 * 1024,
        max_artifacts_per_turn: int = 10,
        retention_hours: float = 24.0,
    ) -> None:
        self.root = Path(root)
        self.db = db
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.max_artifacts_per_turn = max(1, int(max_artifacts_per_turn))
        self.retention_seconds = max(60.0, float(retention_hours) * 3600.0)

    async def initialize(self) -> None:
        await asyncio.to_thread(self.root.mkdir, parents=True, exist_ok=True)

    async def create(
        self,
        data: bytes,
        *,
        kind: str,
        filename: str,
        mime_type: str,
        session_key: str,
        turn_id: str,
        source: str = ArtifactSource.TOOL.value,
        source_name: str = "",
        owner_id: str = "",
        status: str = ArtifactStatus.CANDIDATE.value,
        delivery_eligible: bool = True,
        truncated: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> StoredArtifactRef:
        payload = bytes(data)
        if not payload:
            raise ArtifactStoreError("artifact_empty", "artifact content is empty")
        if len(payload) > self.max_file_bytes:
            raise ArtifactStoreError(
                "artifact_too_large",
                f"artifact exceeds {self.max_file_bytes} bytes",
            )
        if not str(session_key or "").strip() or not str(turn_id or "").strip():
            raise ArtifactStoreError("artifact_scope_missing", "session_key and turn_id are required")
        count = await self.db.count_turn_artifacts(str(session_key), str(turn_id))
        if count >= self.max_artifacts_per_turn:
            raise ArtifactStoreError(
                "artifact_turn_limit",
                f"turn already contains {count} artifacts",
            )

        artifact_id = f"art_{uuid.uuid4().hex}"
        safe_name = _safe_filename(filename, kind, mime_type)
        relative_path = f"{artifact_id}/{safe_name}"
        destination = (self.root / relative_path).resolve()
        root = self.root.resolve()
        if destination.parent.parent != root:
            raise ArtifactStoreError("artifact_path_invalid")
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=False)
        try:
            await asyncio.to_thread(destination.write_bytes, payload)
        except Exception:
            await asyncio.to_thread(_remove_tree, destination.parent)
            raise

        now = time.time()
        ref = StoredArtifactRef(
            artifact_id=artifact_id,
            kind=str(kind or "file"),
            filename=safe_name,
            mime_type=str(mime_type or "application/octet-stream"),
            size_bytes=len(payload),
            content_hash=hashlib.sha256(payload).hexdigest(),
            relative_path=relative_path,
            session_key=str(session_key),
            turn_id=str(turn_id),
            source=str(source or ArtifactSource.TOOL.value),
            source_name=str(source_name or ""),
            owner_id=str(owner_id or ""),
            status=str(status or ArtifactStatus.CANDIDATE.value),
            delivery_eligible=bool(delivery_eligible and not truncated),
            truncated=bool(truncated),
            created_at=now,
            expires_at=now + self.retention_seconds,
            metadata=dict(metadata or {}),
        )
        try:
            await self.db.insert_artifact(ref)
        except Exception:
            await asyncio.to_thread(_remove_tree, destination.parent)
            raise
        return ref

    async def get(self, artifact_id: str) -> StoredArtifactRef | None:
        row = await self.db.artifact_record(str(artifact_id or ""))
        return _ref_from_row(row) if row else None

    async def resolve_path(self, ref: StoredArtifactRef) -> Path:
        root = self.root.resolve()
        path = (root / ref.relative_path).resolve()
        if path != root and root not in path.parents:
            raise ArtifactStoreError("artifact_path_invalid")
        if not path.is_file():
            raise ArtifactStoreError("artifact_missing", f"artifact file is missing: {ref.artifact_id}")
        stat = await asyncio.to_thread(path.stat)
        if stat.st_size != ref.size_bytes:
            raise ArtifactStoreError("artifact_size_changed", f"artifact size changed: {ref.artifact_id}")
        return path

    async def select(self, artifact_id: str, *, session_key: str, turn_id: str) -> StoredArtifactRef:
        ref = await self.get(artifact_id)
        if ref is None:
            raise ArtifactStoreError("artifact_not_found")
        if ref.session_key != session_key or ref.turn_id != turn_id:
            raise ArtifactStoreError("artifact_scope_mismatch")
        if not ref.delivery_eligible or ref.truncated or ref.status == ArtifactStatus.INTERNAL.value:
            raise ArtifactStoreError("artifact_not_deliverable")
        if ref.expires_at and ref.expires_at <= time.time():
            raise ArtifactStoreError("artifact_expired")
        await self.resolve_path(ref)
        await self.db.update_artifact_status(ref.artifact_id, ArtifactStatus.SELECTED.value)
        updated = await self.get(ref.artifact_id)
        return updated or ref

    async def cleanup_expired(self, *, now: float | None = None) -> int:
        rows = await self.db.expired_artifacts(float(now or time.time()))
        removed = 0
        for row in rows:
            ref = _ref_from_row(row)
            await asyncio.to_thread(_remove_tree, (self.root / ref.relative_path).parent)
            await self.db.delete_artifact(ref.artifact_id)
            removed += 1
        return removed


def _safe_filename(value: str, kind: str, mime_type: str) -> str:
    name = Path(str(value or "")).name.strip().replace("\x00", "")
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    if name:
        return name[:180]
    extension = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "audio/mpeg": ".mp3",
        "application/pdf": ".pdf",
    }.get(str(mime_type or "").lower(), "")
    return f"{str(kind or 'artifact')}{extension}"


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_file() or child.is_symlink():
            child.unlink(missing_ok=True)
    path.rmdir()


def _ref_from_row(row: dict[str, Any]) -> StoredArtifactRef:
    import json

    metadata = row.get("metadata_json") or "{}"
    try:
        parsed = json.loads(metadata)
    except (TypeError, ValueError):
        parsed = {}
    return StoredArtifactRef(
        artifact_id=str(row.get("artifact_id") or ""),
        kind=str(row.get("kind") or "file"),
        filename=str(row.get("filename") or "artifact"),
        mime_type=str(row.get("mime_type") or "application/octet-stream"),
        size_bytes=int(row.get("size_bytes") or 0),
        content_hash=str(row.get("content_hash") or ""),
        relative_path=str(row.get("relative_path") or ""),
        session_key=str(row.get("session_key") or ""),
        turn_id=str(row.get("turn_id") or ""),
        source=str(row.get("source") or ArtifactSource.TOOL.value),
        source_name=str(row.get("source_name") or ""),
        owner_id=str(row.get("owner_id") or ""),
        status=str(row.get("status") or ArtifactStatus.CANDIDATE.value),
        delivery_eligible=bool(row.get("delivery_eligible")),
        truncated=bool(row.get("truncated")),
        created_at=float(row.get("created_at") or 0),
        expires_at=float(row.get("expires_at") or 0),
        metadata=parsed if isinstance(parsed, dict) else {},
    )
