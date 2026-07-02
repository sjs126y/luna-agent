"""FileMemoryProvider — reads data/system/*.md into system prompt.

Memory tool (Hermes-style): write to internal (MEMORY.md / USER.md) + external (embedding) simultaneously.
Entries use § separator for multi-line safety.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from personal_agent.memory.base import MemoryProvider

logger = logging.getLogger(__name__)

_SYSTEM_DIR = Path("./data/system")
_SEPARATOR = "\n§\n"

# Per-session profile override — set by Gateway before each agent turn.
# Maps session_key → profile directory name under data/system/.
# e.g. {"wechat:...": "girlfriend"} → loads data/system/girlfriend/
_profile_map: dict[str, str] = {}
_current_session_key: str = ""


def set_system_dir(path: Path) -> None:
    global _SYSTEM_DIR
    _SYSTEM_DIR = path


def set_profile_map(mapping: dict[str, str]) -> None:
    """Set session_key → profile name mapping (from config.yaml)."""
    global _profile_map
    _profile_map = mapping


def set_current_session(session_key: str) -> None:
    """Set current session key for profile-aware memory operations."""
    global _current_session_key
    _current_session_key = session_key


def _get_profile_dir() -> Path | None:
    """Return the profile directory for the current session, or None."""
    profile = _profile_map.get(_current_session_key, "")
    if profile:
        return _SYSTEM_DIR / profile
    return None


class FileMemoryProvider(MemoryProvider):
    """System prompt from data/system/*.md. Also handles internal memory writes."""

    def __init__(self, system_dir: Path | None = None) -> None:
        self._dir = system_dir or _SYSTEM_DIR

    # ── MemoryProvider interface ─────────────────────

    async def prefetch(self, user_message: str) -> list[dict]:
        return []  # system prompt material, no prefetch

    async def save(self, content: str) -> None:
        """Save to MEMORY.md. For USER.md, use save_user()."""
        self._append("MEMORY.md", content)

    async def save_user(self, content: str) -> None:
        """Save to USER.md."""
        self._append("USER.md", content)

    async def search(self, query: str) -> list[str]:
        entries = self._read_entries("MEMORY.md") + self._read_entries("USER.md")
        query_lower = query.lower()
        return [e for e in entries if query_lower in e.lower()]

    async def load_all(self) -> list[str]:
        return self._read_entries("MEMORY.md") + self._read_entries("USER.md")

    async def list_entries(self, *, target: str = "all") -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        targets = _target_names(target)
        for name in targets:
            filename = _target_filename(name)
            for index, text in enumerate(self._read_entries(filename), start=1):
                entries.append({
                    "id": f"{name}:{index}",
                    "index": index,
                    "target": name,
                    "text": text,
                    "path": str(self._dir / filename),
                })
        return entries

    async def search_entries(self, query: str, *, target: str = "all") -> list[dict[str, Any]]:
        query_lower = query.lower()
        return [
            entry
            for entry in await self.list_entries(target=target)
            if query_lower in str(entry.get("text", "")).lower()
        ]

    async def delete(self, identifier: str, *, target: str = "memory") -> bool:
        target_name, index = _parse_identifier(identifier, default_target=target)
        filename = _target_filename(target_name)
        entries = self._read_entries(filename)
        if index < 1 or index > len(entries):
            return False
        del entries[index - 1]
        self._write_entries(filename, entries)
        return True

    def get_system_prompt_text(self) -> str:
        """Combine all .md files from data/system/ into system prompt.

        Profile-aware: if the current session has a profile (e.g. 'girlfriend'),
        loads from data/system/<profile>/ instead of the default directory.
        """
        profile_dir = _get_profile_dir() or self._dir
        if not profile_dir.exists():
            return ""

        parts = []
        for f in sorted(profile_dir.glob("*.md")):
            try:
                text = _read_md(f)
                if text:
                    title = _file_title(f.stem)
                    parts.append(f"## {title}\n\n{text}")
            except Exception:
                logger.exception("Failed to read system file: %s", f)

        return "\n\n".join(parts) if parts else ""

    # ── internals ────────────────────────────────────

    def _append(self, filename: str, content: str) -> None:
        path = self._dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        text = text.strip()
        if text:
            text += _SEPARATOR + content
        else:
            text = content
        path.write_text(text + "\n", encoding="utf-8")
        logger.debug("Appended to %s: %s", filename, content[:60])

    def _read_entries(self, filename: str) -> list[str]:
        path = self._dir / filename
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8")
        entries = []
        for part in text.split(_SEPARATOR):
            part = part.strip()
            if part:
                entries.append(part)
        return entries

    def _write_entries(self, filename: str, entries: list[str]) -> None:
        path = self._dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        text = _SEPARATOR.join(entry.strip() for entry in entries if entry.strip())
        path.write_text((text + "\n") if text else "", encoding="utf-8")

    def health_snapshot(self) -> dict[str, Any]:
        memory_entries = self._read_entries("MEMORY.md")
        user_entries = self._read_entries("USER.md")
        return {
            "provider": type(self).__name__,
            "available": True,
            "entries": len(memory_entries) + len(user_entries),
            "memory_entries": len(memory_entries),
            "user_entries": len(user_entries),
            "system_dir": str(self._dir),
            "profile": _profile_map.get(_current_session_key, ""),
            "last_error": "",
        }


def _read_md(path) -> str:
    """Read a .md file, trying UTF-8, then GBK, then replace bad bytes."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError:
        pass
    try:
        return path.read_text(encoding="gbk").strip()
    except (UnicodeDecodeError, UnicodeError):
        pass
    # Last resort: UTF-8 with bad-byte replacement
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _file_title(stem: str) -> str:
    TITLES = {
        "SOUL": "角色与人格",
        "AGENT": "行为规则",
        "SYSTEM": "系统补充",
        "MEMORY": "用户画像",
        "USER": "用户偏好",
        "IDENTITY": "身份与边界",
        "RELATIONSHIP": "关系状态",
        "INTIMACY": "亲密等级指南",
        "BOOTSTRAP": "引导上下文",
    }
    return TITLES.get(stem.upper(), stem)


def _target_names(target: str) -> list[str]:
    value = str(target or "all").lower()
    if value in {"all", "builtin"}:
        return ["memory", "user"]
    if value in {"memory", "user"}:
        return [value]
    return []


def _target_filename(target: str) -> str:
    value = str(target or "memory").lower()
    if value == "user":
        return "USER.md"
    if value in {"memory", "builtin", "all"}:
        return "MEMORY.md"
    raise ValueError(f"Invalid file memory target: {target}")


def _parse_identifier(identifier: str, *, default_target: str) -> tuple[str, int]:
    raw = str(identifier).strip()
    target = default_target
    value = raw
    if ":" in raw:
        target, value = raw.split(":", 1)
    target = "memory" if target in {"all", "builtin", "external"} else target
    return target, int(value)


# ── memory tool ──────────────────────────────────────

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry


def _get_ext_store():
    try:
        from personal_agent.plugins.builtin.memory.embedding.provider import get_external_instance
        return get_external_instance()
    except Exception:
        return None


async def _memory_tool(action: str, content: str = "", query: str = "",
                       old_text: str = "", target: str = "memory") -> str:
    """Hermes-style memory tool: internal + external simultaneous write.

    Profile-aware: writes to the current session's profile directory
    (e.g. data/system/girlfriend/MEMORY.md) when a profile is active.
    """
    ext = _get_ext_store()
    profile_dir = _get_profile_dir()
    internal = FileMemoryProvider(profile_dir if profile_dir else _SYSTEM_DIR)

    if action == "add":
        if target == "user":
            await internal.save_user(content)
        else:
            await internal.save(content)
        if ext:
            try:
                await ext.save(content)
            except Exception:
                logger.exception("External memory save failed; file memory fallback kept")
        return f"Memory saved to {target}: {content}"
    elif action == "remove":
        return "For now, manage memories via data/system/MEMORY.md or USER.md directly."
    elif action == "search":
        results = await internal.search(query)
        if ext:
            try:
                results = await ext.search(query) + results
            except Exception:
                logger.exception("External memory search failed; falling back to file memory")
        return "\n".join(results) if results else "No matching memories."
    elif action == "list":
        entries = await internal.load_all()
        if ext:
            try:
                entries = await ext.load_all() + entries
            except Exception:
                logger.exception("External memory list failed; falling back to file memory")
        return "\n".join(entries) if entries else "No memories yet."
    return f"Unknown action: {action}. Use 'add', 'search', 'list'."


tool_registry.register(ToolEntry(
    name="memory",
    description="Manage persistent memories. Actions: add (save a fact), search (keyword), list (all). "
                "Use target='user' for user preferences, target='memory' (default) for general memories.",
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "search", "list"]},
            "content": {"type": "string", "description": "Memory content to save (for 'add')"},
            "query": {"type": "string", "description": "Search keyword (for 'search')"},
            "target": {"type": "string", "enum": ["memory", "user"],
                       "description": "Target: 'memory' (MEMORY.md) or 'user' (USER.md). Default 'memory'."},
        },
        "required": ["action"],
    },
    handler=_memory_tool,
    toolset="builtin",
))


# ── ingest tool ──────────────────────────────────────

async def _memory_ingest(path: str) -> str:
    ext = _get_ext_store()
    if ext is None:
        return "External memory not available. Set memory.external_provider=embedding in config.yaml."
    try:
        count = await ext.ingest_file(path)
        return f"Ingested {path}: {count} chunks stored as searchable memories."
    except FileNotFoundError:
        return f"Error: file not found: {path}"
    except ValueError as e:
        return f"Error: {e}"


tool_registry.register(ToolEntry(
    name="memory_ingest",
    description="Ingest a file into external memory. Splits into chunks and stores each as a searchable memory. Supports .txt, .md, .pdf, .docx, .json, .yaml, .py, .csv, .log.",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to file to ingest, relative to workspace"},
        },
        "required": ["path"],
    },
    handler=_memory_ingest,
    toolset="builtin",
    is_parallel_safe=False,
))
