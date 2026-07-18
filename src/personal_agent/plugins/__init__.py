"""Plugin core for Personal Agent."""

from personal_agent.plugins.core.context import PluginRuntimeContext
from personal_agent.plugins.runtime import PluginRuntimeManager
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
    "PluginRuntimeManager",
    "PluginManager",
    "PluginManifest",
    "PluginStatus",
]
