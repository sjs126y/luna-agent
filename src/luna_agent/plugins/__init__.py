"""Plugin core for Luna Agent."""

from luna_agent_plugin_sdk import (
    ActiveRegistration,
    ActiveResourceRequest,
    ActiveRestartPolicy,
    ActiveRunnerState,
    CommandEntry,
    PluginManifest,
    PluginRuntimeContext,
)
from luna_agent.plugins.core.manager import PluginManager
from luna_agent.plugins.core.coordinator import GenerationCoordinator
from luna_agent.plugins.query import PluginQueryService
from luna_agent.plugins.core.models import (
    HookRegistration,
    LoadedPlugin,
    PluginDefinition,
    PluginGeneration,
    PluginStatus,
    PluginView,
)

__all__ = [
    "ActiveRegistration",
    "ActiveResourceRequest",
    "ActiveRestartPolicy",
    "ActiveRunnerState",
    "CommandEntry",
    "HookRegistration",
    "GenerationCoordinator",
    "LoadedPlugin",
    "PluginDefinition",
    "PluginGeneration",
    "PluginRuntimeContext",
    "PluginManager",
    "PluginQueryService",
    "PluginManifest",
    "PluginStatus",
    "PluginView",
]
