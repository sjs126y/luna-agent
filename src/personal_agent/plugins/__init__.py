"""Plugin core for Personal Agent."""

from personal_agent.plugins.context import PluginContext
from personal_agent.plugins.manager import PluginManager
from personal_agent.plugins.models import (
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
    "PluginContext",
    "PluginManager",
    "PluginManifest",
    "PluginStatus",
]
