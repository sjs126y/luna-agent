from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ArtifactStatus(StrEnum):
    INTERNAL = "internal"
    CANDIDATE = "candidate"
    SELECTED = "selected"
    EXPIRED = "expired"


class ArtifactSource(StrEnum):
    TOOL = "tool"
    MCP = "mcp"
    PROVIDER = "provider"
    PLUGIN = "plugin"


def normalize_artifact_kind(kind: str, mime_type: str = "") -> str:
    normalized = str(kind or "file").strip().lower() or "file"
    if normalized not in {"resource", "document"}:
        return normalized
    mime = str(mime_type or "").strip().lower()
    for media_kind in ("image", "audio", "video"):
        if mime.startswith(f"{media_kind}/"):
            return media_kind
    return "file"


@dataclass(frozen=True, slots=True)
class StoredArtifactRef:
    artifact_id: str
    kind: str
    filename: str
    mime_type: str
    size_bytes: int
    content_hash: str
    relative_path: str
    session_key: str
    turn_id: str
    source: str = ArtifactSource.TOOL.value
    source_name: str = ""
    owner_id: str = ""
    status: str = ArtifactStatus.CANDIDATE.value
    delivery_eligible: bool = True
    truncated: bool = False
    created_at: float = 0.0
    expires_at: float = 0.0
    metadata: dict[str, Any] | None = None

    def safe_summary(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "source": self.source,
            "source_name": self.source_name,
            "status": self.status,
            "delivery_eligible": self.delivery_eligible,
            "truncated": self.truncated,
        }
