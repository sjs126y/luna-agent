"""SkillRegistry — module-level singleton. Skills self-register on import."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from personal_agent.skills.entry import SkillEntry

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parent / "builtin"


class SkillRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, SkillEntry] = {}

    def register(self, entry: SkillEntry) -> None:
        self._entries[entry.name] = entry
        logger.debug("Skill registered: %s", entry.name)

    def get(self, name: str) -> SkillEntry | None:
        return self._entries.get(name)

    def list(self) -> list[SkillEntry]:
        return list(self._entries.values())

    def get_summaries(self) -> str:
        """Tier 1: name + one-line description for system prompt."""
        if not self._entries:
            return ""
        lines = ["可用技能："]
        for entry in self._entries.values():
            lines.append(f"- {entry.name}: {entry.description}")
        return "\n".join(lines)

    def load(self, name: str) -> str | None:
        """Tier 2: load full skill markdown content."""
        entry = self._entries.get(name)
        if entry is None:
            return None
        try:
            path = Path(entry.path)
            if not path.is_absolute():
                path = SKILLS_DIR / path
            if path.exists():
                return path.read_text(encoding="utf-8")
        except Exception:
            logger.exception("Failed to load skill: %s", name)
        return None


skill_registry = SkillRegistry()
