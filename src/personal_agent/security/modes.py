"""User-facing modes as permission-profile and approval-policy presets."""

from __future__ import annotations

from dataclasses import dataclass

from personal_agent.security.models import ApprovalPolicy


@dataclass(frozen=True)
class ModePreset:
    id: str
    label: str
    profile: str
    approval_policy: ApprovalPolicy
    auto_approve_cached_tools: bool = False


MODE_PRESETS: dict[str, ModePreset] = {
    "read-only": ModePreset("read-only", "Read Only", "read-only", "never"),
    "ask-first": ModePreset("ask-first", "Ask First", "read-only", "on-request"),
    "local-auto": ModePreset(
        "local-auto",
        "Local Auto",
        "workspace",
        "on-request",
        auto_approve_cached_tools=True,
    ),
    "full-auto": ModePreset(
        "full-auto",
        "Full Auto",
        "trusted",
        "never",
        auto_approve_cached_tools=True,
    ),
}

MODE_ALIASES = {
    "readonly": "read-only",
    "askfirst": "ask-first",
    "localauto": "local-auto",
    "fullauto": "full-auto",
}


def normalize_mode_id(value: object) -> str:
    raw = str(value or "ask-first").strip().lower().replace("_", "-")
    compact = raw.replace("-", "").replace(" ", "")
    if raw in MODE_PRESETS:
        return raw
    return MODE_ALIASES.get(raw, MODE_ALIASES.get(compact, "ask-first"))


def mode_preset(value: object) -> ModePreset:
    return MODE_PRESETS[normalize_mode_id(value)]


def mode_choices() -> tuple[ModePreset, ...]:
    return tuple(MODE_PRESETS.values())
