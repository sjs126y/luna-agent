"""Transactional registration for one preparing plugin generation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Iterable
from typing import Any

from luna_agent.hooks import HookEvent, HookSource
from luna_agent.hooks.specs import hook_spec
from luna_agent.commands.registry import CORE_COMMAND_NAMES
from luna_agent.mcp.models import MCPServerConfig
from luna_agent.mcp.server_registry import RegisteredMCPServer
from luna_agent.memory.provider_registry import MemoryProviderRegistration
from luna_agent.plugins.core.models import HookRegistration
from luna_agent.plugins.runtime.models import CapabilityKind


_NAMED_KINDS = ("tools", "skills", "workflows", "platforms")


@dataclass(frozen=True)
class StagedHookRegistration:
    event: HookEvent
    callback: Any
    name: str
    matcher: str
    priority: int
    timeout_seconds: float
    ordinal: int
    hook_id: str


class RegistrationTransaction:
    """Collect registrations without changing process-global compatibility registries."""

    def __init__(self, manager, plugin) -> None:
        self.manager = manager
        self.plugin = plugin
        self.entries: dict[str, dict[str, Any]] = {
            kind: {} for kind in _NAMED_KINDS
        }
        self.commands: dict[str, Any] = {}
        self.legacy_hooks: list[HookRegistration] = []
        self.typed_hooks: list[StagedHookRegistration] = []
        self.mcp_servers: dict[str, RegisteredMCPServer] = {}
        self.memory_providers: dict[str, MemoryProviderRegistration] = {}
        self._activation_snapshot: dict[str, Any] | None = None
        self._committed = False

    @property
    def committed(self) -> bool:
        return self._committed

    def named(self, kind: str, name: str) -> Any | None:
        return self.entries[kind].get(name)

    def stage_named(self, kind: str, name: str, entry: Any) -> Any:
        if kind not in self.entries:
            raise ValueError(f"Unsupported plugin registration kind: {kind}")
        staged = self.entries[kind].get(name)
        if staged is not None and staged is not entry and staged != entry:
            raise ValueError(
                f"Plugin registered conflicting {kind.rstrip('s')} entries: {name}"
            )
        self.entries[kind][name] = entry
        return entry

    def stage_command(self, entry) -> Any:
        entry.name = str(entry.name or "").lstrip("/")
        if entry.scope not in {"slash", "cli", "both"}:
            raise ValueError(f"Invalid command scope: {entry.scope}")
        if entry.name in CORE_COMMAND_NAMES and entry.scope in {"slash", "both"}:
            raise ValueError(f"Plugin command cannot override core command: /{entry.name}")
        existing = self.manager._commands.get(entry.name)
        if existing is not None and existing.plugin_key != self.plugin.key:
            raise ValueError(f"Plugin command already registered: /{entry.name}")
        staged = self.commands.get(entry.name)
        if staged is not None and staged is not entry and staged != entry:
            raise ValueError(f"Plugin registered conflicting command: /{entry.name}")
        entry.plugin_key = self.plugin.key
        self.commands[entry.name] = entry
        return entry

    def stage_legacy_hook(self, name: str, callback, priority: int) -> HookRegistration:
        registration = HookRegistration(
            plugin_key=self.plugin.key,
            name=str(name),
            callback=callback,
            priority=int(priority),
        )
        self.legacy_hooks.append(registration)
        self.legacy_hooks.sort(key=lambda item: item.priority)
        return registration

    def stage_typed_hook(
        self,
        event: HookEvent | str,
        callback,
        *,
        name: str = "",
        matcher: str = "*",
        priority: int = 100,
        timeout_seconds: float | None = None,
    ) -> StagedHookRegistration:
        normalized_event = event if isinstance(event, HookEvent) else HookEvent(str(event))
        if not callable(callback):
            raise TypeError("Hook callback must be callable")
        normalized_name = str(
            name or getattr(callback, "__name__", "hook") or "hook"
        ).strip()
        normalized_matcher = str(matcher or "*").strip() or "*"
        if normalized_matcher != "*":
            try:
                re.compile(normalized_matcher)
            except re.error as exc:
                raise ValueError(
                    f"Invalid hook matcher '{normalized_matcher}': {exc}"
                ) from exc
        timeout = hook_spec(normalized_event).default_timeout_seconds
        if timeout_seconds is not None:
            timeout = float(timeout_seconds)
        if timeout <= 0 or timeout > 60:
            raise ValueError("Hook timeout must be greater than 0 and at most 60 seconds")
        hook_id = (
            f"{self.plugin.runtime_instance_id}:{normalized_event.value}:"
            f"{normalized_name}"
        )
        if any(item.hook_id == hook_id for item in self.typed_hooks):
            raise ValueError(f"Hook already registered: {hook_id}")
        registration = StagedHookRegistration(
            event=normalized_event,
            callback=callback,
            name=normalized_name,
            matcher=normalized_matcher,
            priority=int(priority),
            timeout_seconds=timeout,
            ordinal=len(self.typed_hooks),
            hook_id=hook_id,
        )
        self.typed_hooks.append(registration)
        return registration

    def stage_mcp_server(self, config: MCPServerConfig | dict[str, Any]) -> MCPServerConfig:
        normalized = (
            config
            if isinstance(config, MCPServerConfig)
            else MCPServerConfig.from_mapping(config)
        )
        current_entries, _revision = self.manager.mcp_server_registry.snapshot()
        existing = current_entries.get(normalized.name)
        if existing is not None and existing.plugin_key != self.plugin.key:
            raise ValueError(
                f"MCP server '{normalized.name}' is already registered by plugin "
                f"'{existing.plugin_key}'"
            )
        staged = self.mcp_servers.get(normalized.name)
        if staged is not None and staged.config != normalized:
            raise ValueError(
                f"MCP server '{normalized.name}' is already registered by this plugin generation"
            )
        self.mcp_servers[normalized.name] = RegisteredMCPServer(
            self.plugin.key,
            normalized,
            self.plugin.runtime_instance_id,
        )
        return normalized

    def stage_memory_provider(self, *, name: str, factory, validator) -> str:
        normalized = str(name or "").strip().lower()
        if not normalized or not normalized.replace("-", "_").isalnum():
            raise ValueError(f"Invalid memory provider name: {name!r}")
        from luna_agent.memory.provider_registry import memory_provider_registry

        existing = memory_provider_registry.get(normalized)
        if existing is not None and existing.plugin_key != self.plugin.key:
            raise ValueError(
                f"Memory provider '{normalized}' is already registered by plugin "
                f"'{existing.plugin_key}'"
            )
        registration = MemoryProviderRegistration(
            name=normalized,
            plugin_key=self.plugin.key,
            factory=factory,
            validator=validator,
        )
        staged = self.memory_providers.get(normalized)
        if staged is not None and staged != registration:
            raise ValueError(
                f"Plugin registered conflicting memory provider: {normalized}"
            )
        self.memory_providers[normalized] = registration
        return normalized

    def capture_import_delta(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> None:
        """Adopt legacy import side effects, then let the caller restore globals."""
        for kind in _NAMED_KINDS:
            previous = before["entries"][kind]
            current = after["entries"][kind]
            changed = {
                name
                for name, entry in current.items()
                if name not in previous or previous[name] is not entry
            }
            for name in sorted(changed):
                entry = current[name]
                self._mark_owner(entry)
                self.stage_named(kind, name, entry)
                registered = getattr(self.plugin, f"{kind}_registered")
                if name not in registered:
                    registered.append(name)

    def activate(
        self,
        *,
        preserve_kinds: Iterable[CapabilityKind] = (),
    ) -> None:
        if self._committed:
            return
        preserved = set(preserve_kinds)
        snapshot = self.manager._registration_snapshot()
        self._activation_snapshot = snapshot
        try:
            self._activate_named_entries(
                preserve_platforms=CapabilityKind.PLATFORM in preserved
            )
            self.manager._remove_plugin_commands(self.plugin.key)
            self.manager._commands.update(self.commands)
            self.manager._remove_plugin_hooks(self.plugin.key)
            for registration in self.legacy_hooks:
                self.manager._hooks.setdefault(registration.name, []).append(registration)
                self.manager._hooks[registration.name].sort(
                    key=lambda item: item.priority
                )
            for registration in self.typed_hooks:
                self.manager.hook_manager.register(
                    owner=self.plugin.runtime_instance_id,
                    source=HookSource.PLUGIN,
                    event=registration.event,
                    callback=registration.callback,
                    name=registration.name,
                    matcher=registration.matcher,
                    priority=registration.priority,
                    timeout_seconds=registration.timeout_seconds,
                    active=False,
                    managed=True,
                )
            self.manager.mcp_server_registry.unregister_plugin(self.plugin.key)
            for registration in self.mcp_servers.values():
                self.manager.mcp_server_registry.register(
                    registration.plugin_key,
                    registration.config,
                    runtime_instance_id=registration.runtime_instance_id,
                )
            from luna_agent.memory.provider_registry import memory_provider_registry

            if CapabilityKind.MEMORY_PROVIDER not in preserved:
                memory_provider_registry.unregister_plugin(self.plugin.key)
                for registration in self.memory_providers.values():
                    memory_provider_registry.register(
                        name=registration.name,
                        plugin_key=registration.plugin_key,
                        factory=registration.factory,
                        validator=registration.validator,
                    )
        except Exception:
            self.manager.hook_manager.unregister_owner(self.plugin.runtime_instance_id)
            self.manager._restore_registration_snapshot(snapshot)
            self._activation_snapshot = None
            raise
        self._committed = True

    def rollback(self) -> None:
        snapshot = self._activation_snapshot
        if snapshot is None:
            return
        self.manager.hook_manager.unregister_owner(self.plugin.runtime_instance_id)
        self.manager._restore_registration_snapshot(snapshot)
        self._activation_snapshot = None
        self._committed = False

    def finalize(self) -> None:
        """Forget rollback state after capability publication succeeds."""
        self._activation_snapshot = None

    def mcp_configs(self) -> list[MCPServerConfig]:
        return [registration.config for registration in self.mcp_servers.values()]

    def _activate_named_entries(self, *, preserve_platforms: bool) -> None:
        from luna_agent.platforms.core import platform_registry
        from luna_agent.skills.registry import skill_registry
        from luna_agent.tools.registry import tool_registry
        from luna_agent.workflow.registry import workflow_registry

        registries = {
            "tools": tool_registry,
            "skills": skill_registry,
            "workflows": workflow_registry,
            "platforms": platform_registry,
        }
        for kind, registry in registries.items():
            if preserve_platforms and kind == "platforms":
                continue
            previous_names = set(
                self._activation_snapshot["entries"][kind]
            ) if self._activation_snapshot is not None else set()
            for name in previous_names - set(self.entries[kind]):
                existing = self._activation_snapshot["entries"][kind].get(name)
                if self._entry_owner(existing) == self.plugin.key:
                    registry.unregister(name)
            for entry in self.entries[kind].values():
                self._mark_owner(entry)
                registry.register(entry)

    def _mark_owner(self, entry: Any | None) -> None:
        if entry is None:
            return
        try:
            setattr(entry, "_plugin_key", self.plugin.key)
        except (AttributeError, TypeError):
            pass
        if hasattr(entry, "plugin_key"):
            try:
                setattr(entry, "plugin_key", self.plugin.key)
            except (AttributeError, TypeError):
                pass

    @staticmethod
    def _entry_owner(entry: Any | None) -> str:
        if entry is None:
            return ""
        return str(
            getattr(entry, "_plugin_key", "")
            or getattr(entry, "plugin_key", "")
            or ""
        )
