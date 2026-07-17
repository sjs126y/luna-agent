from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import unquote, urlparse

from personal_agent.artifacts.models import ArtifactSource, StoredArtifactRef, normalize_artifact_kind
from personal_agent.artifacts.store import ArtifactStore, ArtifactStoreError


async def materialize_tool_artifact(
    store: ArtifactStore,
    artifact,
    *,
    session_key: str,
    turn_id: str,
    tool_name: str,
    result_metadata: dict | None = None,
) -> StoredArtifactRef:
    metadata = dict(getattr(artifact, "metadata", {}) or {})
    truncated = bool(metadata.get("truncated"))
    if truncated:
        raise ArtifactStoreError("artifact_truncated", "truncated artifacts cannot be delivered")

    data = _artifact_bytes(artifact)
    result_metadata = dict(result_metadata or {})
    mcp_server = str(result_metadata.get("mcp_server") or "")
    source = ArtifactSource.MCP.value if mcp_server else ArtifactSource.TOOL.value
    source_name = f"{mcp_server}:{tool_name}" if mcp_server else tool_name
    mime_type = str(getattr(artifact, "mime_type", "") or "application/octet-stream")
    return await store.create(
        data,
        kind=normalize_artifact_kind(getattr(artifact, "kind", ""), mime_type),
        filename=str(getattr(artifact, "name", "") or ""),
        mime_type=mime_type,
        session_key=session_key,
        turn_id=turn_id,
        source=source,
        source_name=source_name,
        truncated=truncated,
        metadata={"source_uri_scheme": _uri_scheme(getattr(artifact, "uri", ""))},
    )


def _artifact_bytes(artifact) -> bytes:
    encoded = str(getattr(artifact, "data", "") or "")
    if encoded:
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ArtifactStoreError("artifact_data_invalid", "artifact data is not valid base64") from exc

    uri = str(getattr(artifact, "uri", "") or "")
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ArtifactStoreError("artifact_content_missing", "artifact has no inline data or file URI")
    path = Path(unquote(parsed.path)).resolve()
    from personal_agent.tools.sandbox import get_sandbox

    error = get_sandbox().check_path(path, access="read")
    if error:
        raise ArtifactStoreError("artifact_path_blocked", error)
    if not path.is_file() or path.is_symlink():
        raise ArtifactStoreError("artifact_file_invalid", "artifact file does not exist or is a symlink")
    return path.read_bytes()


def _uri_scheme(uri: str) -> str:
    return urlparse(str(uri or "")).scheme
