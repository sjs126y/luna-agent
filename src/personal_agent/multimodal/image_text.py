"""Image-to-text fallback abstractions and cache helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import shutil
import time
from typing import Any, Protocol

from personal_agent.attachments.store import ResolvedAttachment
from personal_agent.models.messages import AttachmentRef


@dataclass(frozen=True)
class ImageTextDescription:
    text: str
    method: str = "unknown"
    provider: str = ""
    model: str = ""
    prompt_version: int = 1
    confidence: str = "unknown"
    cached: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ImageTextDescribeUnavailable(RuntimeError):
    def __init__(self, reason: str = "image_text_describer_unavailable", detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail or reason)


class ImageTextDescriber(Protocol):
    async def describe(self, resolved: ResolvedAttachment, ref: AttachmentRef) -> ImageTextDescription:
        ...


class NullImageTextDescriber:
    async def describe(self, resolved: ResolvedAttachment, ref: AttachmentRef) -> ImageTextDescription:
        raise ImageTextDescribeUnavailable("image_text_describer_unavailable")


class ImageTextCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def get(
        self,
        *,
        sha256: str,
        method: str,
        provider: str = "",
        model: str = "",
        prompt_version: int = 1,
    ) -> ImageTextDescription | None:
        path = self._path(
            sha256=sha256,
            method=method,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
        )
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        text = str(data.get("text") or "")
        if not text:
            return None
        return ImageTextDescription(
            text=text,
            method=str(data.get("method") or method),
            provider=str(data.get("provider") or provider),
            model=str(data.get("model") or model),
            prompt_version=int(data.get("prompt_version") or prompt_version),
            confidence=str(data.get("confidence") or "unknown"),
            cached=True,
            metadata=dict(data.get("metadata") or {}),
        )

    def put(
        self,
        description: ImageTextDescription,
        *,
        sha256: str,
        source_mime_type: str = "",
    ) -> Path:
        path = self._path(
            sha256=sha256,
            method=description.method,
            provider=description.provider,
            model=description.model,
            prompt_version=description.prompt_version,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sha256": sha256,
            "kind": "image_text",
            "method": description.method,
            "provider": description.provider,
            "model": description.model,
            "prompt_version": description.prompt_version,
            "text": description.text,
            "created_at": _now(),
            "source_mime_type": source_mime_type,
            "confidence": description.confidence,
            "metadata": dict(description.metadata),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.move(str(tmp), str(path))
        return path

    def _path(
        self,
        *,
        sha256: str,
        method: str,
        provider: str,
        model: str,
        prompt_version: int,
    ) -> Path:
        key = _cache_key(
            sha256=sha256,
            method=method,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
        )
        return self.root / f"{key}.image_text.json"


def _cache_key(
    *,
    sha256: str,
    method: str,
    provider: str,
    model: str,
    prompt_version: int,
) -> str:
    payload = json.dumps(
        {
            "sha256": sha256,
            "method": method,
            "provider": provider,
            "model": model,
            "prompt_version": prompt_version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
