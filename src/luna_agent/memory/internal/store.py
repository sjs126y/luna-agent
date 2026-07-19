"""Profile-aware Markdown snapshots and guarded managed-block updates."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import re
import shutil
import time

from luna_agent.memory.models import (
    InternalMemorySnapshot,
    InternalPatchAction,
    InternalPatchOperation,
)

MANAGED_START = "<!-- luna-managed:start -->"
MANAGED_END = "<!-- luna-managed:end -->"
LEGACY_MANAGED_START = "<!-- lumora-managed:start -->"
LEGACY_MANAGED_END = "<!-- lumora-managed:end -->"
ENTRY_RE = re.compile(r"^-\s*<!--\s*memory:([^>]+)\s*-->\s*(.*)$")
MANAGED_RE = re.compile(
    r"<!--\s*(?P<brand>luna|lumora)-managed:start\s*-->"
    r"\n?(?P<body>.*?)\n?"
    r"<!--\s*(?P=brand)-managed:end\s*-->",
    flags=re.DOTALL,
)
AUTO_FILES = {"USER.MD", "MEMORY.MD"}
REVIEW_FILES = {"RELATIONSHIP.MD", "SOUL.MD", "AGENT.MD"}


class InternalMemoryConflict(RuntimeError):
    pass


class InternalMemoryStore:
    def __init__(self, system_dir: Path, *, profile_map: dict[str, str] | None = None) -> None:
        self.system_dir = Path(system_dir)
        self.profile_map = dict(profile_map or {})
        self._locks: dict[str, asyncio.Lock] = {}

    def profile_for_session(self, session_key: str) -> str:
        return str(self.profile_map.get(session_key) or "default")

    def profile_dir(self, profile: str) -> Path:
        return self.system_dir if not profile or profile == "default" else self.system_dir / profile

    def snapshot(self, *, session_key: str = "", profile: str = "") -> InternalMemorySnapshot:
        resolved = profile or self.profile_for_session(session_key)
        directory = self.profile_dir(resolved)
        parts: list[str] = []
        hashes: dict[str, str] = {}
        if directory.exists():
            for path in sorted(directory.glob("*.md")):
                text = _read_md(path)
                hashes[path.name] = _hash_text(text)
                if text.strip():
                    parts.append(f"## {_file_title(path.stem)}\n\n{text.strip()}")
        digest = hashlib.sha256(
            "\n".join(f"{name}:{value}" for name, value in sorted(hashes.items())).encode("utf-8")
        ).hexdigest()
        revision = int(digest[:15], 16) if hashes else 0
        return InternalMemorySnapshot(
            profile=resolved,
            revision=revision,
            content="\n\n".join(parts),
            file_hashes=hashes,
        )

    async def apply_operations(
        self,
        snapshot: InternalMemorySnapshot,
        operations: list[InternalPatchOperation],
        *,
        allow_review_files: bool = False,
    ) -> InternalMemorySnapshot:
        lock = self._locks.setdefault(snapshot.profile, asyncio.Lock())
        async with lock:
            current = self.snapshot(profile=snapshot.profile)
            if current.file_hashes != snapshot.file_hashes:
                raise InternalMemoryConflict("Internal memory changed after consolidation started")
            grouped: dict[str, list[InternalPatchOperation]] = {}
            for operation in operations:
                target = _validate_target(operation.target_file)
                if target in REVIEW_FILES and not allow_review_files:
                    continue
                if operation.action not in {InternalPatchAction.ADD, InternalPatchAction.UPDATE}:
                    continue
                grouped.setdefault(target, []).append(operation)
            directory = self.profile_dir(snapshot.profile)
            directory.mkdir(parents=True, exist_ok=True)
            for filename, items in grouped.items():
                path = directory / _canonical_filename(filename)
                original = _read_md(path) if path.exists() else ""
                updated = _apply_managed_operations(original, items)
                if updated != original:
                    _backup(path, directory)
                    _write_atomic(path, updated)
            return self.snapshot(profile=snapshot.profile)


def _apply_managed_operations(text: str, operations: list[InternalPatchOperation]) -> str:
    match = MANAGED_RE.search(text)
    entries: list[tuple[str, str]] = []
    if match:
        for line in match.group("body").splitlines():
            entry = ENTRY_RE.match(line.strip())
            if entry:
                entries.append((entry.group(1).strip(), entry.group(2).strip()))
    values = dict(entries)
    order = [entry_id for entry_id, _ in entries]
    for operation in operations:
        entry_id = operation.entry_id.strip() or operation.observation_id
        content = " ".join(operation.content.split())
        if not content:
            continue
        if entry_id not in values:
            order.append(entry_id)
        values[entry_id] = content
    lines = [MANAGED_START]
    lines.extend(f"- <!-- memory:{entry_id} --> {values[entry_id]}" for entry_id in order)
    lines.append(MANAGED_END)
    block = "\n".join(lines)
    if match:
        return (text[:match.start()] + block + text[match.end():]).rstrip() + "\n"
    prefix = text.rstrip()
    return ((prefix + "\n\n") if prefix else "") + block + "\n"


def _validate_target(filename: str) -> str:
    value = Path(str(filename)).name.upper()
    if value not in AUTO_FILES | REVIEW_FILES:
        raise ValueError(f"Unsupported internal memory target: {filename}")
    return value


def _canonical_filename(filename: str) -> str:
    stem, _, suffix = filename.partition(".")
    return f"{stem}.md" if suffix else stem


def _write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _backup(path: Path, directory: Path) -> None:
    if not path.exists():
        return
    history = directory / ".history"
    history.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, history / f"{time.time_ns()}-{path.name}")


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_md(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="gbk")
        except UnicodeError:
            return path.read_text(encoding="utf-8", errors="replace")


def _file_title(stem: str) -> str:
    return {
        "SOUL": "角色与人格", "AGENT": "行为规则", "SYSTEM": "系统补充",
        "MEMORY": "用户画像", "USER": "用户偏好", "IDENTITY": "身份与边界",
        "RELATIONSHIP": "关系状态", "INTIMACY": "亲密等级指南", "BOOTSTRAP": "引导上下文",
    }.get(stem.upper(), stem)
