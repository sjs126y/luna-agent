"""Plugin core for Personal Agent."""

from personal_agent.plugins.active import (
    ActiveRegistration,
    ActiveResourceRequest,
    ActiveRestartPolicy,
    ActiveRunnerState,
)
from personal_agent.plugins.core.context import PluginRuntimeContext
from personal_agent.plugins.core.manager import PluginManager
from personal_agent.plugins.query import PluginQueryService
from personal_agent.plugins.core.models import (
    CommandEntry,
    HookRegistration,
    LoadedPlugin,
    PluginManifest,
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
