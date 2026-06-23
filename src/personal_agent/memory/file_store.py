"""FileMemoryProvider — reads data/system/*.md into system prompt.

Memory tool (Hermes-style): write to internal (MEMORY.md / USER.md) + external (embedding) simultaneously.
Entries use § separator for multi-line safety.
"""

from __future__ import annotations

import logging
from pathlib import Path

from personal_agent.memory.base import MemoryProvider

logger = logging.getLogger(__name__)

_SYSTEM_DIR = Path("./data/system")
_SEPARATOR = "\n§\n"


def set_system_dir(path: Path) -> None:
    global _SYSTEM_DIR
    _SYSTEM_DIR = path


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

    def get_system_prompt_text(self) -> str:
        """Combine all .md files from data/system/ into system prompt."""
        if not self._dir.exists():
            return ""

        parts = []
        for f in sorted(self._dir.glob("*.md")):
            try:
                text = f.read_text(encoding="utf-8").strip()
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


def _file_title(stem: str) -> str:
    TITLES = {
        "SOUL": "角色与人格",
        "AGENT": "行为规则",
        "SYSTEM": "系统补充",
        "MEMORY": "用户画像",
        "USER": "用户偏好",
    }
    return TITLES.get(stem.upper(), stem)


# ── memory tool ──────────────────────────────────────

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry


def _get_ext_store():
    try:
        from personal_agent.memory.embedding_store import get_external_instance
        return get_external_instance()
    except Exception:
        return None


async def _memory_tool(action: str, content: str = "", query: str = "",
                       old_text: str = "", target: str = "memory") -> str:
    """Hermes-style memory tool: internal + external simultaneous write."""
    ext = _get_ext_store()
    internal = FileMemoryProvider()  # always available

    if action == "add":
        if target == "user":
            await internal.save_user(content)
        else:
            await internal.save(content)
        if ext:
            await ext.save(content)
        return f"Memory saved to {target}: {content}"
    elif action == "remove":
        return "For now, manage memories via data/system/MEMORY.md or USER.md directly."
    elif action == "search":
        results = await internal.search(query)
        if ext:
            results = await ext.search(query) + results
        return "\n".join(results) if results else "No matching memories."
    elif action == "list":
        entries = await internal.load_all()
        if ext:
            entries = await ext.load_all() + entries
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
