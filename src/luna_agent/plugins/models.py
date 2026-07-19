"""Compatibility exports for plugin data models."""

from luna_agent.plugins.core.models import (
    CommandEntry,
    HookRegistration,
    LoadedPlugin,
    PluginManifest,
    PluginStatus,
)

__all__ = [
    "CommandEntry",
    "HookRegistration",
    "LoadedPlugin",
    "PluginManifest",
    "PluginStatus",
]
