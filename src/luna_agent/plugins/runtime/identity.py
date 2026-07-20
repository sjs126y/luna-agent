"""Deterministic package and generation identities."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

PLUGIN_API_VERSION = 1
_IGNORED_PARTS = {"__pycache__", ".git", ".pytest_cache"}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


def package_digest(root: Path | None) -> str:
    digest = hashlib.sha256()
    if root is None or not root.exists():
        digest.update(b"missing-package")
        return digest.hexdigest()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        if path.suffix.lower() in _IGNORED_SUFFIXES or not path.is_file():
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def generation_id(
    plugin_key: str,
    package_hash: str,
    config: Mapping[str, Any] | None = None,
    *,
    environment_id: str = "",
) -> str:
    payload = json.dumps(
        {
            "api": PLUGIN_API_VERSION,
            "config": dict(config or {}),
            "environment": str(environment_id or ""),
            "package": package_hash,
            "plugin": plugin_key,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"{plugin_key}@{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def runtime_instance_id(plugin_key: str) -> str:
    normalized = plugin_key.replace("/", "-")
    return f"{normalized}:{uuid4().hex}"
