"""Local attachment cache with path and URL safety checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import mimetypes
from pathlib import Path
import shutil
from typing import Any
from urllib.request import Request, urlopen

from personal_agent.models.messages import AttachmentRef
from personal_agent.tools.sandbox import get_sandbox
from personal_agent.tools.url_safety import check_url

DEFAULT_MAX_BYTES = {
    "image": 20 * 1024 * 1024,
    "audio": 50 * 1024 * 1024,
    "video": 50 * 1024 * 1024,
    "file": 50 * 1024 * 1024,
}


@dataclass
class ResolvedAttachment:
    id: str
    kind: str
    mime_type: str = ""
    name: str = ""
    size: int = 0
    sha256: str = ""
    local_path: str = ""
    source_url: str = ""
    platform_file_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AttachmentStore:
    def __init__(
        self,
        root: Path | str,
        *,
        max_bytes_by_kind: dict[str, int] | None = None,
    ) -> None:
        self.root = Path(root)
        self.max_bytes_by_kind = dict(DEFAULT_MAX_BYTES)
        if max_bytes_by_kind:
            self.max_bytes_by_kind.update(max_bytes_by_kind)
        self.index_path = self.root / "index.json"

    def resolve(self, ref: AttachmentRef) -> ResolvedAttachment:
        if ref.local_path:
            return self.store_local_path(ref.local_path, ref=ref)
        if ref.url:
            return self.store_url(ref.url, ref=ref)
        if ref.platform_file_id:
            raise AttachmentStoreError("platform_file_download_unavailable")
        raise AttachmentStoreError("attachment_has_no_resolvable_location")

    def store_local_path(self, path: str, *, ref: AttachmentRef | None = None) -> ResolvedAttachment:
        source = Path(path).expanduser().resolve()
        sandbox_error = get_sandbox().check_path(source)
        if sandbox_error:
            raise AttachmentStoreError("path_not_allowed", sandbox_error)
        if not source.exists() or not source.is_file():
            raise AttachmentStoreError("file_not_found")
        size = source.stat().st_size
        kind = _kind(ref, source)
        self._check_size(kind, size)
        data = source.read_bytes()
        return self.store_bytes(data, ref=ref, name=_name(ref, source), source_url="")

    def store_url(self, url: str, *, ref: AttachmentRef | None = None) -> ResolvedAttachment:
        safety_error = check_url(url)
        if safety_error:
            raise AttachmentStoreError("unsafe_url", safety_error)
        request = Request(url, headers={"User-Agent": "Personal-Agent/attachment-store"})
        with urlopen(request, timeout=20) as response:
            declared_type = response.headers.get_content_type() or ""
            declared_length = response.headers.get("Content-Length")
            kind = _kind(ref, Path(url))
            if declared_length:
                self._check_size(kind, int(declared_length))
            limit = self._max_bytes(kind)
            data = response.read(limit + 1)
        self._check_size(kind, len(data))
        ref = _with_mime(ref, declared_type)
        return self.store_bytes(data, ref=ref, name=_name(ref, Path(url)), source_url=url)

    def store_bytes(
        self,
        data: bytes,
        *,
        ref: AttachmentRef | None = None,
        name: str = "",
        source_url: str = "",
    ) -> ResolvedAttachment:
        kind = _kind(ref, Path(name or "attachment"))
        self._check_size(kind, len(data))
        digest = hashlib.sha256(data).hexdigest()
        mime_type = _mime_type(ref, name, data)
        suffix = _suffix(name, mime_type)
        directory = self.root / _kind_dir(kind)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{digest}{suffix}"
        if not target.exists():
            target.write_bytes(data)
        resolved = ResolvedAttachment(
            id=digest,
            kind=kind,
            mime_type=mime_type,
            name=name or _name(ref, target),
            size=len(data),
            sha256=digest,
            local_path=str(target),
            source_url=source_url,
            platform_file_id=str(getattr(ref, "platform_file_id", "") or ""),
            metadata=dict(getattr(ref, "metadata", {}) or {}),
        )
        self._write_index(resolved)
        return resolved

    def _check_size(self, kind: str, size: int) -> None:
        limit = self._max_bytes(kind)
        if size > limit:
            raise AttachmentStoreError("size_exceeded", f"{size} > {limit}")

    def _max_bytes(self, kind: str) -> int:
        return int(self.max_bytes_by_kind.get(kind, self.max_bytes_by_kind["file"]))

    def _write_index(self, resolved: ResolvedAttachment) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            index = json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception:
            index = {}
        index[resolved.id] = resolved.as_dict()
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.move(str(tmp), str(self.index_path))


class AttachmentStoreError(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail or reason)


def _kind(ref: AttachmentRef | None, path: Path) -> str:
    value = str(getattr(ref, "kind", "") or "").lower()
    if value in {"image", "audio", "video", "file"}:
        return value
    if value in {"photo", "picture"}:
        return "image"
    mime_type = str(getattr(ref, "mime_type", "") or mimetypes.guess_type(str(path))[0] or "")
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("audio/"):
        return "audio"
    if mime_type.startswith("video/"):
        return "video"
    return "file"


def _kind_dir(kind: str) -> str:
    return {"image": "images", "audio": "audio", "video": "video"}.get(kind, "files")


def _name(ref: AttachmentRef | None, path: Path) -> str:
    return str(getattr(ref, "name", "") or path.name or getattr(ref, "id", "") or "attachment")


def _mime_type(ref: AttachmentRef | None, name: str, data: bytes) -> str:
    explicit = str(getattr(ref, "mime_type", "") or "")
    if explicit:
        return explicit
    guessed = mimetypes.guess_type(name)[0]
    if guessed:
        return guessed
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _suffix(name: str, mime_type: str) -> str:
    suffix = Path(name).suffix
    if suffix:
        return suffix
    return mimetypes.guess_extension(mime_type) or ".bin"


def _with_mime(ref: AttachmentRef | None, mime_type: str) -> AttachmentRef | None:
    if ref is None or ref.mime_type:
        return ref
    return AttachmentRef(
        id=ref.id,
        kind=ref.kind,
        name=ref.name,
        mime_type=mime_type,
        size=ref.size,
        url=ref.url,
        platform_file_id=ref.platform_file_id,
        local_path=ref.local_path,
        metadata=dict(ref.metadata),
    )
