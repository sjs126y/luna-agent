"""Plugin core for Personal Agent."""

from lumora_plugin_sdk import (
    ActiveRegistration,
    ActiveResourceRequest,
    ActiveRestartPolicy,
    ActiveRunnerState,
    CommandEntry,
    PluginManifest,
    PluginRuntimeContext,
)
from personal_agent.plugins.core.manager import PluginManager
from personal_agent.plugins.query import PluginQueryService
from personal_agent.plugins.core.models import (
    HookRegistration,
    LoadedPlugin,
    PluginStatus,
)

__all__ = [
    "ActiveRegistration",
    "ActiveResourceRequest",
    "ActiveRestartPolicy",
    "ActiveRunnerState",
    "CommandEntry",
    "HookRegistration",
    "LoadedPlugin",
    "PluginRuntimeContext",
    "PluginManager",
    "PluginQueryService",
    "PluginManifest",
    "PluginStatus",
]
