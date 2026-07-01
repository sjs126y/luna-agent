"""Plugin data models."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class PluginStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    DISABLED = "DISABLED"
    DEFERRED = "DEFERRED"
    LOADING = "LOADING"
    LOADED = "LOADED"
    ERROR = "ERROR"


CommandHandler = Callable[..., str | Awaitable[str | None] | None]
HookCallback = Callable[..., Any | Awaitable[Any]]


@dataclass
class PluginManifest:
    key: str
    name: str
    version: str
    description: str = ""
    kind: str = "user"
    entrypoint: str = ""
    requires_env: list[str] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)
    enabled_by_default: bool = False
    source: str = "user"
    path: Path | None = None
    deferred: bool = False

    @classmethod
    def from_mapping(
        cls,
        data: dict[str, Any],
        *,
        source: str = "user",
        path: Path | None = None,
    ) -> "PluginManifest":
        missing = [name for name in ("key", "name", "version", "entrypoint") if not data.get(name)]
        if missing:
            raise ValueError(f"Plugin manifest missing required field(s): {', '.join(missing)}")

        requires_env = data.get("requires_env") or []
        provides = data.get("provides") or []
        if isinstance(requires_env, str):
            requires_env = [requires_env]
        if isinstance(provides, str):
            provides = [provides]

        return cls(
            key=str(data["key"]),
            name=str(data["name"]),
            version=str(data["version"]),
            description=str(data.get("description", "")),
            kind=str(data.get("kind", "user")),
            entrypoint=str(data["entrypoint"]),
            requires_env=[str(item) for item in requires_env],
            provides=[str(item) for item in provides],
            enabled_by_default=bool(data.get("enabled_by_default", False)),
            source=str(data.get("source", source)),
            path=Path(path) if path else None,
            deferred=bool(data.get("deferred", False)),
        )


@dataclass
class HookRegistration:
    plugin_key: str
    name: str
    callback: HookCallback
    priority: int = 100


@dataclass
class CommandEntry:
    name: str
    description: str
    handler: CommandHandler
    scope: str = "slash"
    plugin_key: str = ""


@dataclass
class LoadedPlugin:
    key: str
    manifest: PluginManifest
    status: PluginStatus = PluginStatus.DISCOVERED
    module: Any | None = None
    ctx: Any | None = None
    error: str | None = None
    deferred: bool = False
    enabled: bool = False
    tools_registered: list[str] = field(default_factory=list)
    skills_registered: list[str] = field(default_factory=list)
    workflows_registered: list[str] = field(default_factory=list)
    platforms_registered: list[str] = field(default_factory=list)
    mcp_servers_registered: list[str] = field(default_factory=list)
    hooks_registered: list[str] = field(default_factory=list)
    commands_registered: list[str] = field(default_factory=list)
    middleware_registered: list[str] = field(default_factory=list)

    def registration_counts(self) -> dict[str, int]:
        return {
            "tools": len(self.tools_registered),
            "skills": len(self.skills_registered),
            "workflows": len(self.workflows_registered),
            "platforms": len(self.platforms_registered),
            "mcp_servers": len(self.mcp_servers_registered),
            "hooks": len(self.hooks_registered),
            "commands": len(self.commands_registered),
            "middleware": len(self.middleware_registered),
        }
