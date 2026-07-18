"""Plugin data models."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re
from typing import Any

from personal_agent.plugins.runtime.models import PluginRuntimeState


class PluginStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    DISABLED = "DISABLED"
    DEFERRED = "DEFERRED"
    LOADING = "LOADING"
    LOADED = "LOADED"
    ERROR = "ERROR"


CommandHandler = Callable[..., str | Awaitable[str | None] | None]
HookCallback = Callable[..., Any | Awaitable[Any]]

PLUGIN_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*(/[a-z0-9][a-z0-9_.-]*)*$")
PLUGIN_KIND_VALUES = {
    "automation",
    "builtin",
    "development",
    "general",
    "integration",
    "platform",
    "productivity",
    "tool",
    "tools",
    "skill",
    "skills",
    "workflow",
    "memory",
    "llm",
    "mcp",
    "user",
}
PLUGIN_SOURCE_VALUES = {"builtin", "installed", "local", "user"}
PLUGIN_MANIFEST_FIELDS = {
    "schema_version",
    "key",
    "name",
    "version",
    "description",
    "kind",
    "entrypoint",
    "requires_env",
    "provides",
    "tags",
    "enabled_by_default",
    "source",
    "deferred",
    "record_import_delta",
}


@dataclass
class PluginManifest:
    key: str
    name: str
    version: str
    schema_version: int = 1
    description: str = ""
    kind: str = "user"
    entrypoint: str = ""
    requires_env: list[str] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    enabled_by_default: bool = False
    source: str = "local"
    declared_source: str = ""
    path: Path | None = None
    deferred: bool = False
    record_import_delta: bool = True
    unknown_fields: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(
        cls,
        data: dict[str, Any],
        *,
        source: str = "local",
        path: Path | None = None,
    ) -> "PluginManifest":
        if not isinstance(data, dict):
            raise ValueError("Plugin manifest must be an object")

        schema_version = data.get("schema_version", 1)
        if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != 1:
            raise ValueError("Plugin manifest field 'schema_version' must be 1")

        missing = [name for name in ("key", "name", "version", "entrypoint") if not data.get(name)]
        if missing:
            raise ValueError(f"Plugin manifest missing required field(s): {', '.join(missing)}")

        key = str(data["key"])
        if not PLUGIN_KEY_RE.fullmatch(key):
            raise ValueError(
                "Plugin manifest field 'key' must use lowercase segments "
                "like 'builtin/tools' or 'platforms/telegram'"
            )

        entrypoint = str(data["entrypoint"])
        if not _valid_entrypoint(entrypoint):
            raise ValueError("Plugin manifest field 'entrypoint' must be 'module' or 'module:function'")

        kind = str(data.get("kind", "user"))
        if kind not in PLUGIN_KIND_VALUES:
            raise ValueError(f"Plugin manifest field 'kind' must be one of: {', '.join(sorted(PLUGIN_KIND_VALUES))}")

        declared_source = str(data.get("source", source))
        if declared_source not in PLUGIN_SOURCE_VALUES:
            raise ValueError(
                f"Plugin manifest field 'source' must be one of: {', '.join(sorted(PLUGIN_SOURCE_VALUES))}"
            )
        effective_source = source if source in PLUGIN_SOURCE_VALUES else declared_source

        requires_env = _string_list(data["requires_env"] if "requires_env" in data else [], "requires_env")
        provides = _string_list(data["provides"] if "provides" in data else [], "provides")
        tags = _string_list(data["tags"] if "tags" in data else [], "tags")
        enabled_by_default = _bool_field(data, "enabled_by_default", False)
        deferred = _bool_field(data, "deferred", False)
        if deferred and kind != "platform":
            raise ValueError("Plugin manifest field 'deferred' is only supported for platform plugins")
        record_import_delta = _bool_field(data, "record_import_delta", True)
        unknown_fields = sorted(key for key in data if key not in PLUGIN_MANIFEST_FIELDS)

        return cls(
            key=key,
            name=str(data["name"]),
            version=str(data["version"]),
            schema_version=schema_version,
            description=str(data.get("description", "")),
            kind=kind,
            entrypoint=entrypoint,
            requires_env=[str(item) for item in requires_env],
            provides=[str(item) for item in provides],
            tags=[str(item) for item in tags],
            enabled_by_default=enabled_by_default,
            source=effective_source,
            declared_source=declared_source,
            path=Path(path) if path else None,
            deferred=deferred,
            record_import_delta=record_import_delta,
            unknown_fields=unknown_fields,
        )


def _string_list(value: Any, field_name: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ValueError(f"Plugin manifest field '{field_name}' must be a string or list of strings")
    if not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"Plugin manifest field '{field_name}' must contain only non-empty strings")
    return list(value)


def _bool_field(data: dict[str, Any], field_name: str, default: bool) -> bool:
    if field_name not in data:
        return default
    value = data[field_name]
    if not isinstance(value, bool):
        raise ValueError(f"Plugin manifest field '{field_name}' must be a boolean")
    return value


def _valid_entrypoint(value: str) -> bool:
    if not value or value.startswith(":") or value.endswith(":"):
        return False
    module, sep, func = value.partition(":")
    if not _valid_dotted_name(module):
        return False
    return not sep or _valid_identifier(func)


def _valid_dotted_name(value: str) -> bool:
    return all(_valid_identifier(part) for part in value.split("."))


def _valid_identifier(value: str) -> bool:
    return bool(value) and value.isidentifier()


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
    error_traceback: str | None = None
    deferred: bool = False
    enabled: bool = False
    generation_id: str = ""
    runtime_instance_id: str = ""
    module_namespace: str = ""
    package_digest: str = ""
    runtime_state: PluginRuntimeState = PluginRuntimeState.DISCOVERED
    generation_scope: Any | None = None
    active_registration: Any | None = None
    active_runner: Any | None = None
    active_enabled: bool = False
    active_error: str = ""
    tools_registered: list[str] = field(default_factory=list)
    skills_registered: list[str] = field(default_factory=list)
    workflows_registered: list[str] = field(default_factory=list)
    platforms_registered: list[str] = field(default_factory=list)
    mcp_servers_registered: list[str] = field(default_factory=list)
    hooks_registered: list[str] = field(default_factory=list)
    commands_registered: list[str] = field(default_factory=list)
    middleware_registered: list[str] = field(default_factory=list)
    memory_providers_registered: list[str] = field(default_factory=list)

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
            "memory_providers": len(self.memory_providers_registered),
        }
