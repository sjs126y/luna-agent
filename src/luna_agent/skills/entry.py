"""SkillEntry — skill metadata."""

from dataclasses import dataclass, field


@dataclass
class SkillEntry:
    name: str                # "web-dev"
    description: str         # One-line summary for Tier 1 disclosure
    path: str                # Path to skill .md file
    triggers: list[str] = field(default_factory=list)  # ["/web-dev", "/前端"]
    plugin_key: str = ""
    allowed_root: str = ""
