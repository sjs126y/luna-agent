"""Shared attachment normalization for platform adapters."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from luna_agent.models.messages import MessagePart


def canonical_attachment_kind(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"image", "photo", "picture", "img"}:
        return "image"
    if normalized in {"voice", "record", "audio", "sound"}:
        return "audio"
    if normalized in {"video", "movie"}:
        return "video"
    if normalized in {"file", "document", "doc", "attachment"}:
        return "file"
    return normalized or "file"


def attachment_part(
    *,
    kind: str,
    data: dict[str, Any] | None = None,
    text: str = "",
    name: str = "",
    mime_type: str = "",
    size: int = 0,
    url: str = "",
    local_path: str = "",
    platform_file_id: str = "",
    metadata_key: str = "platform_data",
) -> MessagePart:
    payload = dict(data or {})
    resolved_url, resolved_path, resolved_file_id = media_target_fields(
        payload,
        url=url,
        local_path=local_path,
        platform_file_id=platform_file_id,
    )
    resolved_kind = canonical_attachment_kind(kind)
    resolved_name = str(
        name
        or payload.get("name")
        or payload.get("file_name")
        or payload.get("filename")
        or payload.get("fileName")
        or ""
    )
    resolved_mime = str(
        mime_type
        or payload.get("mime_type")
        or payload.get("mimeType")
        or payload.get("mime")
        or ""
    )
    resolved_size = _as_int(size or payload.get("size") or payload.get("file_size") or payload.get("fileSize"))
    detail = str(
        text
        or resolved_name
        or resolved_url
        or resolved_path
        or resolved_file_id
        or payload.get("summary")
        or resolved_kind
    )
    metadata = {metadata_key: payload} if payload else {}
    if resolved_size:
        metadata["size"] = resolved_size
    return MessagePart(
        type=resolved_kind,
        text=detail,
        url=resolved_url,
        path=resolved_path,
        file_id=resolved_file_id,
        name=resolved_name,
        mime_type=resolved_mime,
        metadata=metadata,
    )


def media_target_fields(
    data: dict[str, Any],
    *,
    url: str = "",
    local_path: str = "",
    platform_file_id: str = "",
) -> tuple[str, str, str]:
    raw_url = str(url or data.get("url") or data.get("cdn_url") or data.get("download_url") or "")
    raw_path = str(local_path or "")
    raw_file_id = str(
        platform_file_id
        or data.get("file_id")
        or data.get("fileId")
        or data.get("media_id")
        or data.get("mediaId")
        or data.get("file_key")
        or data.get("fileKey")
        or data.get("image_key")
        or data.get("imageKey")
        or data.get("id")
        or ""
    )
    raw_file = str(data.get("file") or data.get("path") or "")

    if raw_url:
        return raw_url, raw_path, raw_file_id
    if raw_file:
        if _is_url(raw_file):
            return raw_file, raw_path, raw_file_id
        if _is_local_path(raw_file):
            return raw_url, raw_file, raw_file_id
        if not raw_file_id:
            raw_file_id = raw_file
    return raw_url, raw_path, raw_file_id


def _is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"}


def _is_local_path(value: str) -> bool:
    if value.startswith("file://"):
        return True
    if Path(value).is_absolute():
        return True
    return bool(re.match(r"^[A-Za-z]:[\\/]", value))


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
