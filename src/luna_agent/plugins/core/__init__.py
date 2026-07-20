"""Plugin core implementation."""

from luna_agent.plugins.core.context import PluginRuntimeContext
from luna_agent.plugins.core.coordinator import GenerationCoordinator
from luna_agent.plugins.core.manager import PluginManager
from luna_agent.plugins.core.models import (
    CommandEntry,
    HookRegistration,
    LoadedPlugin,
    PluginDefinition,
    PluginGeneration,
    PluginManifest,
    PluginStatus,
    PluginView,
)

__all__ = [
    "CommandEntry",
    "HookRegistration",
    "GenerationCoordinator",
    "LoadedPlugin",
    "PluginDefinition",
    "PluginGeneration",
    "PluginRuntimeContext",
    "PluginManager",
    "PluginManifest",
    "PluginStatus",
    "PluginView",
]
