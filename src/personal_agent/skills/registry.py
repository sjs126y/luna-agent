"""SkillRegistry — module-level singleton. Skills self-register on import.

Skill loading pipeline:
  1. Lookup: name → SkillEntry
  2. Path validation: resolve, prevent traversal outside SKILLS_DIR
  3. Extension check: .md only
  4. Size limit: max 50KB
  5. Read content
  6. Audit log
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from personal_agent.skills.entry import SkillEntry

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parent / "builtin"
MAX_SKILL_BYTES = 50_000


class SkillRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, SkillEntry] = {}
        self._usage_path = Path("data") / "skills" / "usage.json"

    def register(self, entry: SkillEntry) -> None:
        self._entries[entry.name] = entry
        logger.debug("Skill registered: %s", entry.name)

    def unregister(self, name: str) -> None:
        self._entries.pop(name, None)

    def get(self, name: str) -> SkillEntry | None:
        return self._entries.get(name)

    @property
    def usage_path(self) -> Path:
        return self._usage_path

    def set_usage_path(self, path: Path | str) -> None:
        self._usage_path = Path(path)

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
        """Tier 2: load skill content through security pipeline. Returns content or None."""
        entry = self._entries.get(name)
        if entry is None:
            return None

        try:
            # ── 1. Path resolution + traversal prevention ──
            path = Path(entry.path)
            allowed_root = Path(entry.allowed_root) if entry.allowed_root else SKILLS_DIR
            if not path.is_absolute():
                path = allowed_root / path
            path = path.resolve()
            root = allowed_root.resolve()

            if path != root and root not in path.parents:
                logger.warning("Skill path traversal blocked: %s → %s", name, path)
                return None

            # ── 2. Extension check ──
            if path.suffix.lower() != ".md":
                logger.warning("Skill extension blocked: %s (%s)", name, path.suffix)
                return None

            # ── 3. Existence + size check ──
            if not path.exists():
                logger.warning("Skill file not found: %s → %s", name, path)
                return None

            file_size = path.stat().st_size
            if file_size > MAX_SKILL_BYTES:
                logger.warning("Skill too large: %s (%d bytes, max %d)", name, file_size, MAX_SKILL_BYTES)
                return None

            # ── 4. Read ──
            content = path.read_text(encoding="utf-8")
            self._record_usage(name)
            logger.debug("Skill loaded: %s (%d bytes)", name, len(content))

            # ── 5. Audit ──
            from personal_agent.tools.audit import audit_log
            audit_log("skill_load", name, f"{len(content)} bytes", True)

            return content
        except Exception:
            logger.exception("Failed to load skill: %s", name)
            return None

    def _record_usage(self, name: str) -> None:
        """Increment usage counter for this skill."""
        usage_path = self._usage_path
        usage = {}
        if usage_path.exists():
            try:
                usage = json.loads(usage_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        entry = usage.get(name, {"use_count": 0, "last_used": ""})
        entry["use_count"] = entry.get("use_count", 0) + 1
        entry["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        usage[name] = entry
        try:
            usage_path.parent.mkdir(parents=True, exist_ok=True)
            usage_path.write_text(json.dumps(usage, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


skill_registry = SkillRegistry()


def discover_skills(
    skills_dir: Path | None = None,
    registrar=None,
    *,
    recursive: bool = False,
    plugin_key: str = "",
    allowed_root: Path | None = None,
) -> int:
    """Auto-register .md files from a directory. Returns count added."""
    from personal_agent.skills.entry import SkillEntry
    target = Path(skills_dir) if skills_dir else SKILLS_DIR
    if not target.exists():
        return 0
    files = list(target.glob("*.md"))
    if recursive:
        files.extend(target.glob("*/SKILL.md"))
    count = 0
    for f in sorted(set(files)):
        entry = _entry_from_file(
            f,
            plugin_key=plugin_key,
            allowed_root=allowed_root or target,
        )
        name = entry.name
        existing = skill_registry.get(name)
        if existing is not None and registrar is None:
            continue  # don't overwrite explicitly registered skills
        if registrar is not None:
            register_skill = getattr(registrar, "skill", None)
            if not callable(register_skill):
                register_skill = getattr(registrar, "register_skill")
            register_skill(entry)
        else:
            skill_registry.register(entry)
        count += 1
        logger.debug("Auto-discovered skill: %s", name)
    return count


def _entry_from_file(
    path: Path,
    *,
    plugin_key: str = "",
    allowed_root: Path,
) -> SkillEntry:
    from personal_agent.skills.entry import SkillEntry

    if path.stat().st_size > MAX_SKILL_BYTES:
        raise ValueError(f"Skill file is too large: {path}")
    text = path.read_text(encoding="utf-8").strip()
    metadata: dict = {}
    body = text
    if text.startswith("---\n"):
        parts = text.split("\n---\n", 1)
        if len(parts) != 2:
            raise ValueError(f"Skill frontmatter is not terminated: {path}")
        raw_metadata = yaml.safe_load(parts[0][4:]) or {}
        if not isinstance(raw_metadata, dict):
            raise ValueError(f"Skill frontmatter must be an object: {path}")
        metadata = raw_metadata
        body = parts[1].strip()

    default_name = path.parent.name if path.name == "SKILL.md" else path.stem
    name = str(metadata.get("name") or default_name).strip().lower().replace(" ", "-")
    if not name:
        raise ValueError(f"Skill name is empty: {path}")
    description = str(metadata.get("description") or "").strip()
    if not description:
        first_line = next((line for line in body.splitlines() if line.strip()), name)
        description = first_line.lstrip("#").strip()
    raw_triggers = metadata.get("triggers", [])
    if isinstance(raw_triggers, str):
        triggers = [raw_triggers]
    elif isinstance(raw_triggers, list) and all(isinstance(item, str) for item in raw_triggers):
        triggers = list(raw_triggers)
    else:
        raise ValueError(f"Skill triggers must be a string or list of strings: {path}")
    return SkillEntry(
        name=name,
        description=description[:100],
        path=str(path.resolve()),
        triggers=triggers,
        plugin_key=plugin_key,
        allowed_root=str(allowed_root.resolve()),
    )
