"""Plugin discovery, loading, hooks, commands, and diagnostics."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import re
import sys
import traceback
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from personal_agent.persistence.json_store import read_json_object, write_json_atomic
from personal_agent.commands.registry import CORE_COMMAND_NAMES
from personal_agent.mcp.server_registry import MCPServerRegistry
from personal_agent.plugins.core.context import PluginContext
from personal_agent.plugins.core.models import (
    CommandEntry,
    HookRegistration,
    LoadedPlugin,
    PluginManifest,
    PluginStatus,
)
from personal_agent.hooks import HookEvent, HookManager, HookSource

logger = logging.getLogger(__name__)

CORE_SLASH_COMMANDS = set(CORE_COMMAND_NAMES)

_BUILTIN_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "builtin"


class PluginManager:
    def __init__(
        self,
        settings: Any | None = None,
        *,
        plugin_dirs: Iterable[Path] | None = None,
        state_path: Path | None = None,
        include_builtin: bool = True,
        hook_manager: HookManager | None = None,
    ) -> None:
        self.settings = settings
        self._plugins: dict[str, LoadedPlugin] = {}
        self._hooks: dict[str, list[HookRegistration]] = {}
        self._commands: dict[str, CommandEntry] = {}
        self.hook_manager = hook_manager or HookManager()
        self.mcp_server_registry = MCPServerRegistry()
        self._conversation_coordinator = None
        self._delivery_service = None

        configured_dirs = list(getattr(settings, "plugins_dirs", []) or [])
        requested_dirs = list(plugin_dirs) if plugin_dirs is not None else configured_dirs
        base_dirs = [_BUILTIN_PLUGIN_DIR] if include_builtin else []
        self._plugin_dirs = self._dedupe_dirs([*base_dirs, *[Path(p) for p in requested_dirs]])

        data_dir = Path(getattr(settings, "agent_data_dir", "data"))
        self._state_path = Path(state_path) if state_path else data_dir / "plugins" / "state.json"
        self._state = self._load_state()

    @property
    def commands(self) -> dict[str, CommandEntry]:
        return dict(self._commands)

    @property
    def hooks(self) -> dict[str, list[HookRegistration]]:
        return {name: list(items) for name, items in self._hooks.items()}

    def bind_application_ports(self, *, conversation_coordinator, delivery_service) -> None:
        self._conversation_coordinator = conversation_coordinator
        self._delivery_service = delivery_service

    def plugin_conversation_port(self, plugin_key: str):
        if self._conversation_coordinator is None:
            raise RuntimeError("active plugin runtime is unavailable")
        from personal_agent.plugins.core.ports import PluginConversationPort

        return PluginConversationPort(
            plugin=self._plugins[plugin_key],
            coordinator=self._conversation_coordinator,
        )

    def plugin_notification_port(self, plugin_key: str):
        if self._conversation_coordinator is None or self._delivery_service is None:
            raise RuntimeError("active plugin runtime is unavailable")
        from personal_agent.plugins.core.ports import PluginNotificationPort

        return PluginNotificationPort(
            plugin=self._plugins[plugin_key],
            coordinator=self._conversation_coordinator,
            delivery_service=self._delivery_service,
        )

    def discover(self) -> list[LoadedPlugin]:
        for directory in self._plugin_dirs:
            source = self._source_for_directory(Path(directory))
            self._discover_dir(Path(directory), source=source, recursive=True)

        for plugin in self._plugins.values():
            if plugin.status == PluginStatus.ERROR:
                continue
            plugin.enabled = self._resolve_enabled(plugin.manifest)
            if not plugin.enabled and plugin.status != PluginStatus.ERROR:
                plugin.status = PluginStatus.DISABLED
            elif plugin.manifest.deferred and plugin.status == PluginStatus.DISCOVERED:
                plugin.status = PluginStatus.DEFERRED

        return self.list_plugins()

    def load_enabled(self, *, include_deferred: bool = False) -> None:
        if not self._plugins:
            self.discover()
        for plugin in list(self._plugins.values()):
            if not plugin.enabled:
                plugin.status = PluginStatus.DISABLED
                continue
            if plugin.manifest.deferred and not include_deferred:
                if plugin.status not in (PluginStatus.LOADED, PluginStatus.ERROR):
                    plugin.status = PluginStatus.DEFERRED
                continue
            self.load_plugin(plugin.key)

    def load_plugin(self, key: str) -> LoadedPlugin:
        if not self._plugins:
            self.discover()
        plugin = self._plugins[key]
        if plugin.status == PluginStatus.LOADED:
            return plugin
        if not plugin.enabled:
            plugin.status = PluginStatus.DISABLED
            return plugin

        missing_env = self._missing_env(plugin.manifest)
        if missing_env:
            plugin.status = PluginStatus.ERROR
            plugin.error = f"Missing required env: {', '.join(missing_env)}"
            plugin.error_traceback = None
            return plugin

        plugin.status = PluginStatus.LOADING
        plugin.error = None
        plugin.error_traceback = None
        before = self._registration_snapshot()
        try:
            plugin.ctx = PluginContext(self, plugin)
            module, register_fn = self._import_entrypoint(plugin.manifest)
            plugin.module = module
            if register_fn is not None:
                result = register_fn(plugin.ctx)
                if inspect.isawaitable(result):
                    raise RuntimeError("Async plugin register() is not supported during synchronous load")
            elif hasattr(module, "register"):
                result = module.register(plugin.ctx)
                if inspect.isawaitable(result):
                    raise RuntimeError("Async plugin register() is not supported during synchronous load")
            after = self._registration_snapshot()
            self._assert_no_registry_replacements(before, after, plugin.key)
            if plugin.manifest.record_import_delta:
                self._record_registry_delta(plugin, before["names"], after["names"])
            plugin.status = PluginStatus.LOADED
            counts = plugin.registration_counts()
            logger.info(
                "Plugin loaded: %s skills=%d mcp=%d hooks=%d commands=%d",
                plugin.key,
                counts["skills"],
                counts["mcp_servers"],
                counts["hooks"],
                counts["commands"],
            )
        except Exception as exc:
            self.hook_manager.unregister_owner(plugin.key)
            self._restore_registration_snapshot(before)
            self._clear_plugin_registrations(plugin)
            plugin.module = None
            plugin.ctx = None
            plugin.status = PluginStatus.ERROR
            plugin.error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            plugin.error_traceback = traceback.format_exc()
            logger.exception("Plugin '%s' failed to load", key)
        return plugin

    def ensure_registration_available(
        self,
        kind: str,
        name: str,
        existing: Any | None,
        candidate: Any,
        plugin_key: str,
    ) -> bool:
        """Reject cross-plugin replacement while allowing legacy idempotent registration."""
        if existing is None:
            return True
        owner = self._registration_owner(kind, name) or str(
            getattr(existing, "_plugin_key", "") or getattr(existing, "plugin_key", "")
        )
        if owner and owner != plugin_key:
            raise ValueError(f"{kind.title()} '{name}' is already registered by plugin '{owner}'")
        if owner == plugin_key:
            return existing is not candidate
        if existing is candidate or (not owner and existing == candidate):
            return False
        label = owner or "core runtime"
        raise ValueError(f"{kind.title()} '{name}' is already registered by {label}")

    def unload_plugin(self, key: str) -> LoadedPlugin:
        plugin = self._plugins[key]
        self._remove_plugin_commands(key)
        self._remove_plugin_hooks(key)
        self.hook_manager.unregister_owner(key)

        from personal_agent.platforms.core import platform_registry
        from personal_agent.skills.registry import skill_registry
        from personal_agent.tools.registry import tool_registry
        from personal_agent.workflow.registry import workflow_registry
        from personal_agent.memory.provider_registry import memory_provider_registry

        for name in list(plugin.tools_registered):
            tool_registry.unregister(name)
        for name in list(plugin.skills_registered):
            skill_registry.unregister(name)
        for name in list(plugin.workflows_registered):
            workflow_registry.unregister(name)
        for name in list(plugin.platforms_registered):
            platform_registry.unregister(name)

        self.mcp_server_registry.unregister_plugin(key)
        memory_provider_registry.unregister_plugin(key)
        self._clear_plugin_registrations(plugin)
        plugin.module = None
        plugin.ctx = None
        plugin.error = None
        plugin.error_traceback = None
        plugin.status = PluginStatus.DISABLED if not plugin.enabled else PluginStatus.DISCOVERED
        return plugin

    def enable_plugin(self, key: str) -> LoadedPlugin:
        if not self._plugins:
            self.discover()
        plugin = self._plugins[key]
        self._state.setdefault("enabled", [])
        self._state.setdefault("disabled", [])
        if key not in self._state["enabled"]:
            self._state["enabled"].append(key)
        self._state["disabled"] = [item for item in self._state["disabled"] if item != key]
        self._save_state()
        plugin.enabled = True
        plugin.status = PluginStatus.DEFERRED if plugin.manifest.deferred else PluginStatus.DISCOVERED
        return plugin

    def disable_plugin(self, key: str) -> LoadedPlugin:
        if not self._plugins:
            self.discover()
        plugin = self._plugins[key]
        self._state.setdefault("enabled", [])
        self._state.setdefault("disabled", [])
        if key not in self._state["disabled"]:
            self._state["disabled"].append(key)
        self._state["enabled"] = [item for item in self._state["enabled"] if item != key]
        self._save_state()
        plugin.enabled = False
        return self.unload_plugin(key)

    def register_hook(self, plugin_key: str, name: str, callback, priority: int = 100) -> None:
        reg = HookRegistration(plugin_key=plugin_key, name=name, callback=callback, priority=priority)
        self._hooks.setdefault(name, []).append(reg)
        self._hooks[name].sort(key=lambda item: item.priority)

    def register_event_hook(
        self,
        plugin_key: str,
        event: HookEvent | str,
        callback,
        *,
        name: str = "",
        matcher: str = "*",
        priority: int = 100,
        timeout_seconds: float | None = None,
    ):
        return self.hook_manager.register(
            owner=plugin_key,
            source=HookSource.PLUGIN,
            event=event,
            callback=callback,
            name=name,
            matcher=matcher,
            priority=priority,
            timeout_seconds=timeout_seconds,
        )

    async def invoke_hook(self, name: str, *args, **kwargs) -> Any:
        result = None
        for reg in self._hooks.get(name, []):
            try:
                value = reg.callback(*args, **kwargs)
                if inspect.isawaitable(value):
                    value = await value
                if value is not None:
                    result = value
            except Exception:
                logger.exception("Plugin hook failed: plugin=%s hook=%s", reg.plugin_key, name)
        if result is None and args:
            return args[0]
        return result

    def register_command(self, entry: CommandEntry) -> None:
        entry.name = entry.name.lstrip("/")
        if entry.scope not in {"slash", "cli", "both"}:
            raise ValueError(f"Invalid command scope: {entry.scope}")
        if entry.name in CORE_SLASH_COMMANDS and entry.scope in {"slash", "both"}:
            raise ValueError(f"Plugin command cannot override core command: /{entry.name}")
        existing = self._commands.get(entry.name)
        if existing and existing.plugin_key != entry.plugin_key:
            raise ValueError(f"Plugin command already registered: /{entry.name}")
        self._commands[entry.name] = entry

    def get_command(self, name: str, *, scope: str = "slash") -> CommandEntry | None:
        entry = self._commands.get(name.lstrip("/"))
        if entry is None:
            return None
        if entry.scope not in {scope, "both"}:
            return None
        return entry

    async def execute_command(self, name: str, **kwargs) -> str | None:
        entry = self.get_command(name, scope=kwargs.pop("scope", "slash"))
        if entry is None:
            return None
        value = entry.handler(**kwargs)
        if inspect.isawaitable(value):
            value = await value
        return None if value is None else str(value)

    def register_mcp_server(self, plugin_key: str, config: Any):
        return self.mcp_server_registry.register(plugin_key, config)

    def get_mcp_servers(self) -> list[Any]:
        return self.mcp_server_registry.configs()

    def list_plugins(self) -> list[LoadedPlugin]:
        return [self._plugins[key] for key in sorted(self._plugins)]

    def doctor_plugin(self, key: str, *, check_entrypoint: bool | None = None) -> dict[str, Any]:
        if not self._plugins:
            self.discover()
        plugin = self._plugins[key]
        missing_env = self._missing_env(plugin.manifest)
        manifest_error = (plugin.error or "") if plugin.manifest.entrypoint == "invalid" else ""
        entrypoint_checked = False
        if manifest_error:
            entrypoint_ok, entrypoint_error = False, ""
        elif check_entrypoint is False:
            entrypoint_ok, entrypoint_error = True, ""
        elif check_entrypoint is None and plugin.status == PluginStatus.DEFERRED:
            entrypoint_ok, entrypoint_error = True, ""
        else:
            entrypoint_checked = True
            entrypoint_ok, entrypoint_error = self._check_entrypoint(plugin.manifest)
        return {
            "key": plugin.key,
            "name": plugin.manifest.name,
            "version": plugin.manifest.version,
            "schema_version": plugin.manifest.schema_version,
            "description": plugin.manifest.description,
            "kind": plugin.manifest.kind,
            "entrypoint": plugin.manifest.entrypoint,
            "provides": plugin.manifest.provides,
            "tags": plugin.manifest.tags,
            "enabled_by_default": plugin.manifest.enabled_by_default,
            "enabled": plugin.enabled,
            "status": plugin.status.value,
            "deferred": plugin.deferred,
            "source": plugin.manifest.source,
            "declared_source": plugin.manifest.declared_source or plugin.manifest.source,
            "path": str(plugin.manifest.path) if plugin.manifest.path else "",
            "manifest_path": self._manifest_path(plugin),
            "source_boundary": self._source_boundary(plugin),
            "requires_env": plugin.manifest.requires_env,
            "missing_env": missing_env,
            "manifest_valid": not manifest_error,
            "manifest_error": manifest_error,
            "manifest_unknown_fields": list(plugin.manifest.unknown_fields),
            "manifest_warnings": self._manifest_warnings(plugin),
            "boundary_warnings": self._boundary_warnings(plugin),
            "entrypoint_checked": entrypoint_checked,
            "entrypoint_importable": entrypoint_ok,
            "entrypoint_error": entrypoint_error,
            "deferred_reason": self._deferred_reason(plugin),
            "error": plugin.error or "",
            "error_traceback": plugin.error_traceback or "",
            "registered": plugin.registration_counts(),
            "registered_items": self._registered_items(plugin),
            "diagnostic_hints": self._diagnostic_hints(plugin, missing_env, entrypoint_ok, entrypoint_error),
        }

    def validate_plugin_path(self, path: Path, *, load: bool = True) -> dict[str, Any]:
        manifest_path = self._resolve_plugin_manifest_path(Path(path))
        plugin_dir = manifest_path.parent
        if not self._plugins:
            self.discover()

        matches = [
            plugin
            for plugin in self._plugins.values()
            if plugin.manifest.path and self._same_path(plugin.manifest.path, plugin_dir)
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Expected exactly one plugin manifest at {plugin_dir}, found {len(matches)}"
            )

        plugin = matches[0]
        if plugin.manifest.entrypoint != "invalid":
            plugin.enabled = True
            if plugin.status == PluginStatus.DISABLED:
                plugin.status = PluginStatus.DEFERRED if plugin.deferred else PluginStatus.DISCOVERED
            if load:
                plugin = self.load_plugin(plugin.key)

        report = self.doctor_plugin(plugin.key, check_entrypoint=True)
        report["validation_path"] = str(plugin_dir)
        report["validation_manifest"] = str(manifest_path)
        report["validation_load_requested"] = load
        report["validation_loaded"] = report["status"] == PluginStatus.LOADED.value
        report["validation_ok"] = (
            report["manifest_valid"]
            and report["entrypoint_importable"]
            and not report["missing_env"]
            and report["status"] != PluginStatus.ERROR.value
        )
        return report

    def _add_manifest(self, manifest: PluginManifest) -> None:
        if manifest.key in self._plugins:
            existing = self._plugins[manifest.key]
            if (
                existing.manifest.path is not None
                and manifest.path is not None
                and self._same_path(existing.manifest.path, manifest.path)
            ):
                return
            existing.status = PluginStatus.ERROR
            existing.error = f"Duplicate plugin key: {manifest.key}"
            existing.error_traceback = None
            return
        boundary_error = self._manifest_boundary_error(manifest)
        enabled = self._resolve_enabled(manifest)
        status = PluginStatus.ERROR if boundary_error else PluginStatus.DISABLED
        if enabled and not boundary_error:
            status = PluginStatus.DEFERRED if manifest.deferred else PluginStatus.DISCOVERED
        self._plugins[manifest.key] = LoadedPlugin(
            key=manifest.key,
            manifest=manifest,
            status=status,
            deferred=manifest.deferred,
            enabled=enabled and not boundary_error,
            error=boundary_error or None,
        )

    def _discover_dir(
        self,
        directory: Path,
        *,
        source: str | None = None,
        recursive: bool = False,
    ) -> None:
        if not directory.exists():
            return
        manifest_files: list[Path] = []
        if recursive:
            manifest_files = sorted(
                path
                for path in directory.rglob("*")
                if path.is_file() and path.name in {"plugin.yaml", "plugin.yml", "plugin.json"}
            )
        else:
            for child in sorted(directory.iterdir()):
                if child.is_dir():
                    for name in ("plugin.yaml", "plugin.yml", "plugin.json"):
                        candidate = child / name
                        if candidate.exists():
                            manifest_files.append(candidate)
                            break
                elif child.name in {"plugin.yaml", "plugin.yml", "plugin.json"}:
                    manifest_files.append(child)

        for manifest_path in manifest_files:
            try:
                data = self._read_manifest_file(manifest_path)
                manifest = PluginManifest.from_mapping(
                    data,
                    source=source or "local",
                    path=manifest_path.parent,
                )
                self._add_manifest(manifest)
            except Exception as exc:
                key = self._invalid_manifest_key(manifest_path)
                manifest = PluginManifest(
                    key=key,
                    name=manifest_path.parent.name,
                    version="0",
                    entrypoint="invalid",
                    source=source or "local",
                    path=manifest_path.parent,
                )
                self._plugins[key] = LoadedPlugin(
                    key=key,
                    manifest=manifest,
                    status=PluginStatus.ERROR,
                    error=str(exc),
                    enabled=False,
                )

    def _read_manifest_file(self, path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            data = json.loads(text)
        else:
            data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError("Plugin manifest must be an object")
        return data

    def _resolve_plugin_manifest_path(self, path: Path) -> Path:
        target = path.expanduser()
        if not target.exists():
            raise ValueError(f"Plugin path does not exist: {target}")
        if target.is_file():
            if target.name not in {"plugin.yaml", "plugin.yml", "plugin.json"}:
                raise ValueError(f"Plugin manifest file must be plugin.yaml, plugin.yml, or plugin.json: {target}")
            return target

        direct = [
            target / name
            for name in ("plugin.yaml", "plugin.yml", "plugin.json")
            if (target / name).is_file()
        ]
        if len(direct) == 1:
            return direct[0]
        if len(direct) > 1:
            raise ValueError(f"Plugin directory has multiple manifest files: {target}")

        nested = sorted(
            item
            for item in target.rglob("*")
            if item.is_file() and item.name in {"plugin.yaml", "plugin.yml", "plugin.json"}
        )
        if not nested:
            raise ValueError(f"Plugin manifest not found under: {target}")
        if len(nested) > 1:
            choices = ", ".join(str(item) for item in nested[:5])
            raise ValueError(f"Plugin path contains multiple manifests; specify one plugin directory: {choices}")
        return nested[0]

    def _invalid_manifest_key(self, manifest_path: Path) -> str:
        raw_base = (manifest_path.parent.name or "manifest").lower()
        base = re.sub(r"[^a-z0-9_.-]+", "-", raw_base).strip("-._") or "manifest"
        key = f"invalid/{base}"
        if key not in self._plugins:
            return key
        index = 2
        while f"{key}-{index}" in self._plugins:
            index += 1
        return f"{key}-{index}"

    def _deferred_reason(self, plugin: LoadedPlugin) -> str:
        if not plugin.deferred:
            return ""
        if plugin.manifest.kind == "platform":
            return "平台插件会在网关解析平台适配器时加载"
        return ""

    def _diagnostic_hints(
        self,
        plugin: LoadedPlugin,
        missing_env: list[str],
        entrypoint_ok: bool,
        entrypoint_error: str,
    ) -> list[str]:
        hints: list[str] = []
        if plugin.manifest.entrypoint == "invalid":
            hints.append(f"修复插件 manifest: {plugin.error or 'invalid manifest'}")
            return hints
        if missing_env:
            hints.append(f"设置缺失环境变量: {', '.join(missing_env)}")
        if not entrypoint_ok:
            hints.append(f"修复入口导入: {entrypoint_error}")
        if plugin.status == PluginStatus.ERROR and plugin.error:
            hints.append(f"修复插件加载错误: {plugin.error}")
        if plugin.status == PluginStatus.DEFERRED:
            hints.append(self._deferred_reason(plugin))
        if not plugin.enabled:
            hints.append("插件已被配置或状态禁用")
        hints.extend(self._manifest_warnings(plugin))
        hints.extend(self._boundary_warnings(plugin))
        return [hint for hint in hints if hint]

    def _manifest_warnings(self, plugin: LoadedPlugin) -> list[str]:
        manifest = plugin.manifest
        if manifest.entrypoint == "invalid":
            return []
        warnings: list[str] = []
        if manifest.unknown_fields:
            warnings.append(f"Manifest 包含未知字段: {', '.join(manifest.unknown_fields)}")
        provides = set(manifest.provides)
        if manifest.kind == "platform" and not provides.intersection({"platform", "platforms"}):
            warnings.append("kind 为 platform 时建议 provides 包含 platform。")
        if manifest.kind == "mcp" and "mcp" not in provides:
            warnings.append("kind 为 mcp 时建议 provides 包含 mcp。")
        if manifest.kind == "platform" and not manifest.deferred:
            warnings.append("platform 插件建议设置 deferred: true，避免启动时 eager import。")
        bad_env = [
            name for name in manifest.requires_env
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name)
        ]
        if bad_env:
            warnings.append(f"requires_env 建议使用大写环境变量名: {', '.join(bad_env)}")
        return self._dedupe_strings(warnings)

    def _boundary_warnings(self, plugin: LoadedPlugin) -> list[str]:
        manifest = plugin.manifest
        if manifest.entrypoint == "invalid":
            return []
        warnings: list[str] = []
        boundary = self._source_boundary(plugin)
        declared = manifest.declared_source or manifest.source
        if declared != manifest.source:
            warnings.append(
                f"Manifest 声明 source={declared}，实际按扫描边界识别为 {manifest.source}。"
            )
        if boundary != "unknown" and manifest.source != boundary:
            warnings.append(
                f"Manifest source={manifest.source} 与路径边界 {boundary} 不一致。"
            )
        if manifest.source != "builtin" and manifest.kind == "builtin":
            warnings.append("用户插件不应声明 kind: builtin。")
        if manifest.source != "builtin" and manifest.key.startswith("builtin/"):
            warnings.append("用户插件不能使用 builtin/* 插件 key。")
        return self._dedupe_strings(warnings)

    def _manifest_boundary_error(self, manifest: PluginManifest) -> str:
        if manifest.source != "builtin" and manifest.key.startswith("builtin/"):
            return f"User plugin cannot use reserved builtin key: {manifest.key}"
        return ""

    @staticmethod
    def _dedupe_strings(items: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in items:
            if item in seen:
                continue
            result.append(item)
            seen.add(item)
        return result

    @staticmethod
    def _dedupe_dirs(directories: Iterable[Path]) -> list[Path]:
        seen: set[Path] = set()
        result: list[Path] = []
        for directory in directories:
            path = Path(directory).expanduser()
            try:
                key = path.resolve()
            except OSError:
                key = path.absolute()
            if key in seen:
                continue
            seen.add(key)
            result.append(path)
        return result

    def _resolve_enabled(self, manifest: PluginManifest) -> bool:
        enabled = set(getattr(self.settings, "plugins_enabled", []) or [])
        disabled = set(getattr(self.settings, "plugins_disabled", []) or [])
        enabled.update(self._state.get("enabled", []))
        disabled.update(self._state.get("disabled", []))
        if manifest.key in disabled:
            return False
        if manifest.key in enabled:
            return True
        return manifest.enabled_by_default

    def _load_state(self) -> dict[str, list[str]]:
        data = read_json_object(self._state_path, {"enabled": [], "disabled": []})
        return {
            "enabled": list(data.get("enabled", [])),
            "disabled": list(data.get("disabled", [])),
        }

    def _save_state(self) -> None:
        write_json_atomic(self._state_path, self._state)

    def _missing_env(self, manifest: PluginManifest) -> list[str]:
        missing = []
        for name in manifest.requires_env:
            resolver = getattr(self.settings, "get_env", None)
            value = resolver(name, "") if callable(resolver) else ""
            if value:
                continue
            setting_name = name.lower()
            if hasattr(self.settings, setting_name) and getattr(self.settings, setting_name):
                continue
            missing.append(name)
        return missing

    def _import_entrypoint(self, manifest: PluginManifest) -> tuple[ModuleType, Any | None]:
        entrypoint = manifest.entrypoint
        module_name, _, func_name = entrypoint.partition(":")
        for path in self._import_paths_for_manifest(manifest):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
        module = importlib.import_module(module_name)
        fn = getattr(module, func_name) if func_name else None
        return module, fn

    def _import_paths_for_manifest(self, manifest: PluginManifest) -> list[Path]:
        paths: list[Path] = []
        if manifest.path:
            paths.append(manifest.path)
            if (manifest.path / "__init__.py").is_file():
                paths.append(manifest.path.parent)
        return paths

    def _check_entrypoint(self, manifest: PluginManifest) -> tuple[bool, str]:
        try:
            module, fn = self._import_entrypoint(manifest)
            if ":" in manifest.entrypoint and fn is None:
                return False, f"Entrypoint function not found: {manifest.entrypoint}"
            if fn is not None and not callable(fn):
                return False, f"Entrypoint is not callable: {manifest.entrypoint}"
            if fn is None and not hasattr(module, "register"):
                return False, "Module has no register() function"
            return True, ""
        except Exception as exc:
            return False, "".join(traceback.format_exception_only(type(exc), exc)).strip()

    def _registered_items(self, plugin: LoadedPlugin) -> dict[str, list[str]]:
        return {
            "tools": list(plugin.tools_registered),
            "skills": list(plugin.skills_registered),
            "workflows": list(plugin.workflows_registered),
            "platforms": list(plugin.platforms_registered),
            "mcp_servers": list(plugin.mcp_servers_registered),
            "hooks": list(plugin.hooks_registered),
            "commands": list(plugin.commands_registered),
            "middleware": list(plugin.middleware_registered),
            "memory_providers": list(plugin.memory_providers_registered),
        }

    def _manifest_path(self, plugin: LoadedPlugin) -> str:
        if plugin.manifest.path is None:
            return ""
        for name in ("plugin.yaml", "plugin.yml", "plugin.json"):
            path = plugin.manifest.path / name
            if path.exists():
                return str(path)
        return ""

    def _source_boundary(self, plugin: LoadedPlugin) -> str:
        if plugin.manifest.path is None:
            return "unknown"
        if self._same_path(plugin.manifest.path, _BUILTIN_PLUGIN_DIR) or _BUILTIN_PLUGIN_DIR in plugin.manifest.path.parents:
            return "builtin"
        return self._source_for_directory(plugin.manifest.path)

    def _source_for_directory(self, directory: Path) -> str:
        if self._same_path(directory, _BUILTIN_PLUGIN_DIR) or _BUILTIN_PLUGIN_DIR in directory.parents:
            return "builtin"
        installed_root = Path(getattr(self.settings, "agent_data_dir", "data")) / "plugins"
        try:
            resolved = directory.resolve()
            installed = installed_root.resolve()
        except OSError:
            resolved = directory.absolute()
            installed = installed_root.absolute()
        if resolved == installed or installed in resolved.parents:
            return "installed"
        return "local"

    def _registry_snapshot(self) -> dict[str, set[str]]:
        from personal_agent.platforms.core import platform_registry
        from personal_agent.skills.registry import skill_registry
        from personal_agent.tools.registry import tool_registry
        from personal_agent.workflow.registry import workflow_registry

        return {
            "tools": tool_registry.all_names,
            "skills": {entry.name for entry in skill_registry.list()},
            "workflows": set(workflow_registry.list_names()),
            "platforms": {entry.name for entry in platform_registry.list()},
        }

    def _registration_snapshot(self) -> dict[str, Any]:
        from personal_agent.memory.provider_registry import memory_provider_registry
        from personal_agent.platforms.core import platform_registry
        from personal_agent.skills.registry import skill_registry
        from personal_agent.tools.registry import tool_registry
        from personal_agent.workflow.registry import workflow_registry

        entries = {
            "tools": {name: tool_registry.get(name) for name in tool_registry.all_names},
            "skills": {entry.name: entry for entry in skill_registry.list()},
            "workflows": {name: workflow_registry.get(name) for name in workflow_registry.list_names()},
            "platforms": {entry.name: entry for entry in platform_registry.list()},
            "memory_providers": {
                name: memory_provider_registry.get(name) for name in memory_provider_registry.names()
            },
        }
        return {
            "entries": entries,
            "names": {kind: set(values) for kind, values in entries.items() if kind != "memory_providers"},
            "commands": dict(self._commands),
            "hooks": {name: list(items) for name, items in self._hooks.items()},
            "mcp_servers": self.mcp_server_registry.snapshot(),
        }

    def _restore_registration_snapshot(self, snapshot: dict[str, Any]) -> None:
        from personal_agent.memory.provider_registry import memory_provider_registry
        from personal_agent.platforms.core import platform_registry
        from personal_agent.skills.registry import skill_registry
        from personal_agent.tools.registry import tool_registry
        from personal_agent.workflow.registry import workflow_registry

        entries = snapshot["entries"]
        self._restore_entry_map(
            entries["tools"],
            {name: tool_registry.get(name) for name in tool_registry.all_names},
            unregister=tool_registry.unregister,
            register=tool_registry.register,
        )
        self._restore_entry_map(
            entries["skills"],
            {entry.name: entry for entry in skill_registry.list()},
            unregister=skill_registry.unregister,
            register=skill_registry.register,
        )
        self._restore_entry_map(
            entries["workflows"],
            {name: workflow_registry.get(name) for name in workflow_registry.list_names()},
            unregister=workflow_registry.unregister,
            register=workflow_registry.register,
        )
        self._restore_entry_map(
            entries["platforms"],
            {entry.name: entry for entry in platform_registry.list()},
            unregister=platform_registry.unregister,
            register=platform_registry.register,
        )
        current_memory = {
            name: memory_provider_registry.get(name) for name in memory_provider_registry.names()
        }
        for name in set(current_memory) - set(entries["memory_providers"]):
            memory_provider_registry.unregister(name)
        for name, registration in entries["memory_providers"].items():
            if current_memory.get(name) is registration:
                continue
            memory_provider_registry.register(
                name=registration.name,
                plugin_key=registration.plugin_key,
                factory=registration.factory,
                validator=registration.validator,
            )

        self._commands = dict(snapshot["commands"])
        self._hooks = {name: list(items) for name, items in snapshot["hooks"].items()}
        self.mcp_server_registry.restore(snapshot["mcp_servers"])

    @staticmethod
    def _restore_entry_map(previous, current, *, unregister, register) -> None:
        for name in set(current) - set(previous):
            unregister(name)
        for name, entry in previous.items():
            if current.get(name) is not entry:
                register(entry)

    @staticmethod
    def _assert_no_registry_replacements(
        before: dict[str, Any],
        after: dict[str, Any],
        plugin_key: str,
    ) -> None:
        for kind, previous in before["entries"].items():
            current = after["entries"].get(kind, {})
            for name, entry in previous.items():
                if name in current and current[name] is not entry:
                    previous_owner = str(
                        getattr(entry, "_plugin_key", "") or getattr(entry, "plugin_key", "")
                    )
                    current_entry = current[name]
                    current_owner = str(
                        getattr(current_entry, "_plugin_key", "")
                        or getattr(current_entry, "plugin_key", "")
                    )
                    if previous_owner == plugin_key or current_owner == plugin_key:
                        continue
                    raise ValueError(f"Plugin replaced existing {kind.rstrip('s')} registration: {name}")

    def _record_registry_delta(
        self,
        plugin: LoadedPlugin,
        before: dict[str, set[str]],
        after: dict[str, set[str]],
    ) -> None:
        self._extend_unique(plugin.tools_registered, sorted(after["tools"] - before["tools"]))
        self._extend_unique(plugin.skills_registered, sorted(after["skills"] - before["skills"]))
        self._extend_unique(plugin.workflows_registered, sorted(after["workflows"] - before["workflows"]))
        self._extend_unique(plugin.platforms_registered, sorted(after["platforms"] - before["platforms"]))

    def _registration_owner(self, kind: str, name: str) -> str:
        attribute = {
            "tool": "tools_registered",
            "skill": "skills_registered",
            "workflow": "workflows_registered",
            "platform": "platforms_registered",
        }.get(kind)
        if attribute is None:
            return ""
        for plugin in self._plugins.values():
            if name in getattr(plugin, attribute):
                return plugin.key
        return ""

    @staticmethod
    def _clear_plugin_registrations(plugin: LoadedPlugin) -> None:
        plugin.tools_registered.clear()
        plugin.skills_registered.clear()
        plugin.workflows_registered.clear()
        plugin.platforms_registered.clear()
        plugin.mcp_servers_registered.clear()
        plugin.hooks_registered.clear()
        plugin.commands_registered.clear()
        plugin.middleware_registered.clear()
        plugin.memory_providers_registered.clear()

    def _extend_unique(self, target: list[str], values: list[str]) -> None:
        for value in values:
            if value not in target:
                target.append(value)

    def _remove_plugin_commands(self, plugin_key: str) -> None:
        for name, entry in list(self._commands.items()):
            if entry.plugin_key == plugin_key:
                del self._commands[name]

    def _remove_plugin_hooks(self, plugin_key: str) -> None:
        for name, regs in list(self._hooks.items()):
            self._hooks[name] = [reg for reg in regs if reg.plugin_key != plugin_key]
            if not self._hooks[name]:
                del self._hooks[name]

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        try:
            return left.resolve() == right.resolve()
        except OSError:
            return left.absolute() == right.absolute()


def run_async(coro):
    return asyncio.run(coro)
