"""Plugin core implementation."""

from personal_agent.plugins.core.context import PluginRuntimeContext
from personal_agent.plugins.core.manager import PluginManager
from personal_agent.plugins.core.models import (
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
    "PluginRuntimeContext",
    "PluginManager",
    "PluginManifest",
    "PluginStatus",
]
