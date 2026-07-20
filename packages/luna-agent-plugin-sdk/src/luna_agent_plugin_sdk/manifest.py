"""Plugin manifest and dependency contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

PLUGIN_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*(/[a-z0-9][a-z0-9_.-]*)*$")
PLUGIN_KIND_VALUES = {
    "automation", "builtin", "development", "general", "integration", "platform",
    "productivity", "tool", "tools", "skill", "skills", "workflow", "memory",
    "llm", "mcp", "user",
}
PLUGIN_SOURCE_VALUES = {"builtin", "installed", "local", "user"}
PLUGIN_MANIFEST_FIELDS = {
    "schema_version", "plugin_api", "key", "name", "version", "description", "kind",
    "entrypoint", "requires_env", "requires", "provides", "tags", "enabled_by_default",
    "source", "deferred", "record_import_delta",
}


@dataclass(frozen=True, slots=True)
class PluginRequirement:
    key: str
    version: str = ""

    def __post_init__(self) -> None:
        if not PLUGIN_KEY_RE.fullmatch(self.key):
            raise ValueError(f"Invalid plugin dependency key: {self.key}")
        _validate_specifier(self.version, f"requires.plugins.{self.key}.version")


@dataclass(frozen=True, slots=True)
class PluginDependencies:
    luna_agent: str = ""
    sdk: str = ""
    python: tuple[str, ...] = ()
    plugins: tuple[PluginRequirement, ...] = ()
    capabilities: tuple[str, ...] = ()
    mcp_tools: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Any) -> "PluginDependencies":
        if value in (None, {}):
            return cls()
        if not isinstance(value, dict):
            raise ValueError("Plugin manifest field 'requires' must be an object")
        unknown = sorted(
            set(value) - {
                "luna_agent", "lumora", "sdk", "python", "plugins",
                "capabilities", "mcp_tools",
            }
        )
        if unknown:
            raise ValueError("Unknown plugin dependency field(s): " + ", ".join(unknown))
        if value.get("luna_agent") and value.get("lumora"):
            raise ValueError("Use only requires.luna_agent; requires.lumora is a legacy alias")
        luna_agent = str(value.get("luna_agent") or value.get("lumora") or "").strip()
        sdk = str(value.get("sdk") or "").strip()
        _validate_specifier(luna_agent, "requires.luna_agent")
        _validate_specifier(sdk, "requires.sdk")
        python = _python_requirements(value.get("python", []))
        raw_plugins = value.get("plugins", [])
        if not isinstance(raw_plugins, list):
            raise ValueError("Plugin manifest field 'requires.plugins' must be a list")
        plugins = []
        for index, item in enumerate(raw_plugins):
            if isinstance(item, str):
                plugins.append(PluginRequirement(item))
                continue
            if not isinstance(item, dict) or not item.get("key"):
                raise ValueError(f"requires.plugins[{index}] must contain key")
            plugins.append(PluginRequirement(
                key=str(item["key"]),
                version=str(item.get("version") or "").strip(),
            ))
        capabilities = _string_tuple(value.get("capabilities", []), "requires.capabilities")
        raw_mcp = value.get("mcp_tools", {})
        if not isinstance(raw_mcp, dict):
            raise ValueError("Plugin manifest field 'requires.mcp_tools' must be an object")
        mcp_tools = {
            str(server or "").strip(): _string_tuple(tools, f"requires.mcp_tools.{server}")
            for server, tools in raw_mcp.items()
        }
        if any(not name for name in mcp_tools):
            raise ValueError("MCP dependency server names must not be empty")
        keys = [item.key for item in plugins]
        if len(keys) != len(set(keys)):
            raise ValueError("Plugin dependency keys must be unique")
        return cls(luna_agent, sdk, python, tuple(plugins), capabilities, mcp_tools)

    @property
    def lumora(self) -> str:
        """Compatibility alias for manifests created before the Luna Agent rename."""
        return self.luna_agent

    def as_dict(self) -> dict[str, Any]:
        return {
            "luna_agent": self.luna_agent,
            "sdk": self.sdk,
            "python": list(self.python),
            "plugins": [
                {"key": item.key, "version": item.version}
                for item in self.plugins
            ],
            "capabilities": list(self.capabilities),
            "mcp_tools": {name: list(tools) for name, tools in self.mcp_tools.items()},
        }


@dataclass
class PluginManifest:
    key: str
    name: str
    version: str
    schema_version: int = 1
    plugin_api: str = ""
    description: str = ""
    kind: str = "user"
    entrypoint: str = ""
    requires_env: list[str] = field(default_factory=list)
    requires: PluginDependencies = field(default_factory=PluginDependencies)
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
        plugin_api = str(data.get("plugin_api") or "").strip()
        _validate_specifier(plugin_api, "plugin_api")
        version = str(data["version"])
        try:
            Version(version)
        except InvalidVersion as exc:
            raise ValueError(f"Plugin manifest field 'version' is invalid: {version}") from exc
        deferred = _bool_field(data, "deferred", False)
        if deferred and kind != "platform":
            raise ValueError("Plugin manifest field 'deferred' is only supported for platform plugins")
        return cls(
            key=key,
            name=str(data["name"]),
            version=version,
            schema_version=schema_version,
            plugin_api=plugin_api,
            description=str(data.get("description", "")),
            kind=kind,
            entrypoint=entrypoint,
            requires_env=list(_string_tuple(data.get("requires_env", []), "requires_env")),
            requires=PluginDependencies.from_mapping(data.get("requires", {})),
            provides=list(_string_tuple(data.get("provides", []), "provides")),
            tags=list(_string_tuple(data.get("tags", []), "tags")),
            enabled_by_default=_bool_field(data, "enabled_by_default", False),
            source=effective_source,
            declared_source=declared_source,
            path=Path(path) if path else None,
            deferred=deferred,
            record_import_delta=_bool_field(data, "record_import_delta", True),
            unknown_fields=sorted(name for name in data if name not in PLUGIN_MANIFEST_FIELDS),
        )


CommandHandler = Callable[..., str | Awaitable[str | None] | None]


@dataclass
class CommandEntry:
    name: str
    description: str
    handler: CommandHandler
    scope: str = "slash"
    plugin_key: str = ""


def _validate_specifier(value: str, field_name: str) -> None:
    if not value:
        return
    try:
        SpecifierSet(value)
    except InvalidSpecifier as exc:
        raise ValueError(f"Plugin manifest field '{field_name}' has invalid version range: {value}") from exc


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"Plugin manifest field '{field_name}' must be a string or list of strings")
    if not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"Plugin manifest field '{field_name}' must contain only non-empty strings")
    return tuple(dict.fromkeys(value))


def _python_requirements(value: Any) -> tuple[str, ...]:
    values = _string_tuple(value, "requires.python") if value else ()
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if raw.startswith(("-", ".", "/")):
            raise ValueError(f"Unsupported Python dependency: {raw}")
        try:
            requirement = Requirement(raw)
        except InvalidRequirement as exc:
            raise ValueError(f"Invalid Python dependency: {raw}") from exc
        if requirement.url:
            raise ValueError(f"Direct URL Python dependencies are not supported: {raw}")
        canonical = str(requirement)
        key = requirement.name.lower().replace("_", "-")
        if key in seen:
            raise ValueError(f"Duplicate Python dependency: {requirement.name}")
        seen.add(key)
        normalized.append(canonical)
    return tuple(sorted(normalized, key=str.lower))


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
    return _valid_dotted_name(module) and (not sep or _valid_identifier(func))


def _valid_dotted_name(value: str) -> bool:
    return all(_valid_identifier(part) for part in value.split("."))


def _valid_identifier(value: str) -> bool:
    return bool(value) and value.isidentifier()
