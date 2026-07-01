"""Plugin discovery, loading, hooks, commands, and diagnostics."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import os
import sys
import traceback
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from personal_agent.plugins.core.context import PluginContext
from personal_agent.plugins.core.models import (
    CommandEntry,
    HookRegistration,
    LoadedPlugin,
    PluginManifest,
    PluginStatus,
)

logger = logging.getLogger(__name__)

CORE_SLASH_COMMANDS = {
    "stop",
    "allow",
    "new",
    "session",
    "usage",
}

_BUILTIN_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "builtin"


class PluginManager:
    def __init__(
        self,
        settings: Any | None = None,
        *,
        plugin_dirs: Iterable[Path] | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self._plugins: dict[str, LoadedPlugin] = {}
        self._hooks: dict[str, list[HookRegistration]] = {}
        self._commands: dict[str, CommandEntry] = {}
        self._mcp_servers: dict[str, list[Any]] = {}
        self._env = self._load_env_file()

        configured_dirs = list(getattr(settings, "plugins_dirs", []) or [])
        requested_dirs = list(plugin_dirs) if plugin_dirs is not None else configured_dirs
        self._plugin_dirs = self._dedupe_dirs([_BUILTIN_PLUGIN_DIR, *[Path(p) for p in requested_dirs]])

        data_dir = Path(getattr(settings, "agent_data_dir", "data"))
        self._state_path = Path(state_path) if state_path else data_dir / "plugins" / "state.json"
        self._state = self._load_state()

    @property
    def commands(self) -> dict[str, CommandEntry]:
        return dict(self._commands)

    @property
    def hooks(self) -> dict[str, list[HookRegistration]]:
        return {name: list(items) for name, items in self._hooks.items()}

    def discover(self) -> list[LoadedPlugin]:
        for directory in self._plugin_dirs:
            self._discover_dir(Path(directory), recursive=True)

        for plugin in self._plugins.values():
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
        plugin.ctx = PluginContext(self, plugin)
        before = self._registry_snapshot()
        try:
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
            if plugin.manifest.record_import_delta:
                self._record_registry_delta(plugin, before, self._registry_snapshot())
            plugin.status = PluginStatus.LOADED
        except Exception as exc:
            plugin.status = PluginStatus.ERROR
            plugin.error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            plugin.error_traceback = traceback.format_exc()
            logger.exception("Plugin '%s' failed to load", key)
        return plugin

    def unload_plugin(self, key: str) -> LoadedPlugin:
        plugin = self._plugins[key]
        self._remove_plugin_commands(key)
        self._remove_plugin_hooks(key)

        from personal_agent.adapters.base import platform_registry
        from personal_agent.skills.registry import skill_registry
        from personal_agent.tools.registry import tool_registry
        from personal_agent.workflow.registry import workflow_registry

        for name in list(plugin.tools_registered):
            tool_registry.unregister(name)
        for name in list(plugin.skills_registered):
            skill_registry.unregister(name)
        for name in list(plugin.workflows_registered):
            workflow_registry.unregister(name)
        for name in list(plugin.platforms_registered):
            platform_registry.unregister(name)

        self._mcp_servers.pop(key, None)
        plugin.tools_registered.clear()
        plugin.skills_registered.clear()
        plugin.workflows_registered.clear()
        plugin.platforms_registered.clear()
        plugin.mcp_servers_registered.clear()
        plugin.hooks_registered.clear()
        plugin.commands_registered.clear()
        plugin.middleware_registered.clear()
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

    def register_mcp_server(self, plugin_key: str, config: Any) -> None:
        self._mcp_servers.setdefault(plugin_key, []).append(config)

    def get_mcp_servers(self) -> list[Any]:
        result: list[Any] = []
        for configs in self._mcp_servers.values():
            result.extend(configs)
        return result

    def list_plugins(self) -> list[LoadedPlugin]:
        return [self._plugins[key] for key in sorted(self._plugins)]

    def doctor_plugin(self, key: str) -> dict[str, Any]:
        if not self._plugins:
            self.discover()
        plugin = self._plugins[key]
        missing_env = self._missing_env(plugin.manifest)
        entrypoint_ok, entrypoint_error = self._check_entrypoint(plugin.manifest)
        return {
            "key": plugin.key,
            "name": plugin.manifest.name,
            "version": plugin.manifest.version,
            "description": plugin.manifest.description,
            "kind": plugin.manifest.kind,
            "entrypoint": plugin.manifest.entrypoint,
            "provides": plugin.manifest.provides,
            "enabled_by_default": plugin.manifest.enabled_by_default,
            "enabled": plugin.enabled,
            "status": plugin.status.value,
            "deferred": plugin.deferred,
            "source": plugin.manifest.source,
            "path": str(plugin.manifest.path) if plugin.manifest.path else "",
            "requires_env": plugin.manifest.requires_env,
            "missing_env": missing_env,
            "entrypoint_importable": entrypoint_ok,
            "entrypoint_error": entrypoint_error,
            "error": plugin.error or "",
            "error_traceback": plugin.error_traceback or "",
            "registered": plugin.registration_counts(),
            "registered_items": self._registered_items(plugin),
        }

    def _add_manifest(self, manifest: PluginManifest) -> None:
        if manifest.key in self._plugins:
            existing = self._plugins[manifest.key]
            existing.status = PluginStatus.ERROR
            existing.error = f"Duplicate plugin key: {manifest.key}"
            return
        enabled = self._resolve_enabled(manifest)
        status = PluginStatus.DISABLED
        if enabled:
            status = PluginStatus.DEFERRED if manifest.deferred else PluginStatus.DISCOVERED
        self._plugins[manifest.key] = LoadedPlugin(
            key=manifest.key,
            manifest=manifest,
            status=status,
            deferred=manifest.deferred,
            enabled=enabled,
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
                    source=source or str(data.get("source", "user")),
                    path=manifest_path.parent,
                )
                self._add_manifest(manifest)
            except Exception as exc:
                key = f"invalid/{manifest_path.parent.name}"
                manifest = PluginManifest(
                    key=key,
                    name=manifest_path.parent.name,
                    version="0",
                    entrypoint="invalid",
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
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {
                        "enabled": list(data.get("enabled", [])),
                        "disabled": list(data.get("disabled", [])),
                    }
        except Exception:
            logger.exception("Failed to read plugin state: %s", self._state_path)
        return {"enabled": [], "disabled": []}

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")

    def _load_env_file(self) -> dict[str, str]:
        env_path = Path(".env")
        if not env_path.exists():
            return {}
        try:
            from dotenv import dotenv_values

            return {key: value or "" for key, value in dotenv_values(env_path).items()}
        except Exception:
            return {}

    def _missing_env(self, manifest: PluginManifest) -> list[str]:
        missing = []
        for name in manifest.requires_env:
            value = os.environ.get(name) or self._env.get(name)
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
        }

    def _registry_snapshot(self) -> dict[str, set[str]]:
        from personal_agent.adapters.base import platform_registry
        from personal_agent.skills.registry import skill_registry
        from personal_agent.tools.registry import tool_registry
        from personal_agent.workflow.registry import workflow_registry

        return {
            "tools": tool_registry.all_names,
            "skills": {entry.name for entry in skill_registry.list()},
            "workflows": set(workflow_registry.list_names()),
            "platforms": {entry.name for entry in platform_registry.list()},
        }

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


def run_async(coro):
    return asyncio.run(coro)
