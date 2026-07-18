"""Bounded directory navigation and file metadata tools."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import mimetypes
import os
from pathlib import Path
import time

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry
from personal_agent.tools.sandbox import get_sandbox


DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 500
MAX_DIRECTORY_SCAN_ENTRIES = 10_000
DIRECTORY_SCAN_TIMEOUT_SECONDS = 5.0


async def _list_directory(
    path: str = ".",
    offset: int = 0,
    limit: int = DEFAULT_LIST_LIMIT,
    include_hidden: bool = False,
) -> str:
    """List one directory level with bounded, structured output."""
    try:
        offset = int(offset)
        limit = int(limit)
    except (TypeError, ValueError):
        return "Error: offset and limit must be integers"
    if offset < 0:
        return "Error: offset must be at least 0"
    if limit < 1 or limit > MAX_LIST_LIMIT:
        return f"Error: limit must be between 1 and {MAX_LIST_LIMIT}"

    sandbox = get_sandbox()
    directory = sandbox.resolve(path)
    error = sandbox.check_path(directory)
    if error:
        return error
    if not directory.exists():
        return f"Error: path not found: {path}"
    if not directory.is_dir():
        return f"Error: '{path}' is not a directory"

    try:
        payload = await asyncio.to_thread(
            _list_directory_sync,
            directory,
            sandbox,
            offset=offset,
            limit=limit,
            include_hidden=bool(include_hidden),
        )
    except Exception as exc:
        return f"Error: {exc}"
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _list_directory_sync(
    directory: Path,
    sandbox,
    *,
    offset: int,
    limit: int,
    include_hidden: bool,
) -> dict:
    deadline = time.monotonic() + DIRECTORY_SCAN_TIMEOUT_SECONDS
    visible: list[os.DirEntry] = []
    scanned = 0
    scan_truncated = False

    with os.scandir(directory) as iterator:
        for entry in iterator:
            scanned += 1
            if scanned > MAX_DIRECTORY_SCAN_ENTRIES or time.monotonic() >= deadline:
                scan_truncated = True
                break
            if entry.name.startswith(".") and not include_hidden:
                continue
            candidate = Path(entry.path)
            if sandbox.check_path(candidate):
                continue
            visible.append(entry)

    visible.sort(key=lambda entry: entry.name.casefold())
    selected = visible[offset:offset + limit]
    entries = [_directory_entry_payload(entry) for entry in selected]
    has_more = offset + len(selected) < len(visible) or scan_truncated
    return {
        "path": str(directory),
        "entries": entries,
        "offset": offset,
        "limit": limit,
        "returned": len(entries),
        "next_offset": offset + len(entries) if has_more else None,
        "truncated": has_more,
        "scan_truncated": scan_truncated,
        "scanned_entries": scanned,
    }


def _directory_entry_payload(entry: os.DirEntry) -> dict:
    try:
        if entry.is_symlink():
            entry_type = "symlink"
            stat = entry.stat(follow_symlinks=False)
        elif entry.is_dir(follow_symlinks=False):
            entry_type = "directory"
            stat = entry.stat(follow_symlinks=False)
        elif entry.is_file(follow_symlinks=False):
            entry_type = "file"
            stat = entry.stat(follow_symlinks=False)
        else:
            entry_type = "other"
            stat = entry.stat(follow_symlinks=False)
    except OSError:
        return {"name": entry.name, "type": "unavailable"}

    payload = {
        "name": entry.name,
        "type": entry_type,
        "modified_at": _format_timestamp(stat.st_mtime),
    }
    if entry_type == "file":
        payload["size_bytes"] = stat.st_size
    return payload


async def _file_info(path: str) -> str:
    """Return safe metadata for a known file or directory."""
    sandbox = get_sandbox()
    target = sandbox.resolve(path)
    error = sandbox.check_path(target)
    if error:
        return error
    if not target.exists():
        return f"Error: path not found: {path}"

    try:
        payload = await asyncio.to_thread(_file_info_sync, target, sandbox)
    except Exception as exc:
        return f"Error: {exc}"
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _file_info_sync(target: Path, sandbox) -> dict:
    stat = target.stat()
    if target.is_file():
        entry_type = "file"
    elif target.is_dir():
        entry_type = "directory"
    else:
        entry_type = "other"

    mime_type, _ = mimetypes.guess_type(target.name)
    payload = {
        "path": str(target),
        "name": target.name,
        "type": entry_type,
        "size_bytes": stat.st_size if entry_type == "file" else None,
        "modified_at": _format_timestamp(stat.st_mtime),
        "mime_type": mime_type if entry_type == "file" else None,
        "content_kind": _content_kind(target, mime_type) if entry_type == "file" else None,
        "readable": True,
        "writable": sandbox.check_path(target, access="write") is None,
    }
    return payload


def _content_kind(path: Path, mime_type: str | None) -> str:
    if mime_type and (
        mime_type.startswith("text/")
        or mime_type in {"application/json", "application/xml", "application/yaml"}
    ):
        return "text"
    try:
        with path.open("rb") as handle:
            sample = handle.read(8192)
    except OSError:
        return "unknown"
    if b"\x00" in sample:
        return "binary"
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    return "text"


def _format_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


tool_registry.register(ToolEntry(
    name="list_directory",
    description="List the immediate contents of one directory with pagination and metadata.",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to list, default '.'"},
            "offset": {"type": "integer", "minimum": 0, "description": "Zero-based result offset, default 0"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_LIMIT, "description": "Maximum entries to return, default 100"},
            "include_hidden": {"type": "boolean", "description": "Include ordinary hidden entries, default false"},
        },
    },
    handler=_list_directory,
    toolset="builtin",
    permission_category="read",
    tags=["file", "directory", "read"],
    risk_level="low",
    usage_hint="Use to browse one directory level before recursive glob or grep searches.",
    timeout_seconds=7,
))


tool_registry.register(ToolEntry(
    name="file_info",
    description="Inspect size, type, timestamps, MIME type, and access for a known path.",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Known file or directory path"},
        },
        "required": ["path"],
    },
    handler=_file_info,
    toolset="builtin",
    permission_category="read",
    tags=["file", "metadata", "read"],
    risk_level="low",
    usage_hint="Use before reading a large or unfamiliar file, or to verify a known path.",
    timeout_seconds=7,
))
