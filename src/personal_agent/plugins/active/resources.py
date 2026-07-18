"""Generation-bound host resources exposed to active plugin runners."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from personal_agent.plugins.active.contracts import ActiveResourceRequest


_BLOCKED_TOOL_CATEGORIES = {"write", "bash", "background"}
_BLOCKED_TOOL_NAMES = {
    "write",
    "edit",
    "bash",
    "process_start",
    "process_kill",
}
_BLOCKED_TOOL_TAGS = {
    "code-execution",
    "destructive",
    "filesystem-write",
    "process-control",
    "shell",
}


class PluginResourceFacade:
    """Resolve only resources declared by one active plugin generation."""

    def __init__(self, *, manager, plugin, request: ActiveResourceRequest) -> None:
        self._manager = manager
        self._plugin = plugin
        self._request = request
        allowed_mcp = {
            f"mcp__{server}__{tool}"
            for server, tools in request.mcp.items()
            for tool in tools
        }
        self._tool = ActiveToolPort(
            manager=manager,
            plugin=plugin,
            allowed=set(request.tools),
        )
        self._mcp = ActiveMCPPort(
            tool_port=ActiveToolPort(
                manager=manager,
                plugin=plugin,
                allowed=allowed_mcp,
            ),
            request=request,
        )

    @property
    def tool(self) -> "ActiveToolPort":
        self._validate()
        return self._tool

    @property
    def mcp(self) -> "ActiveMCPPort":
        self._validate()
        return self._mcp

    @property
    def conversation(self):
        self._require("conversation", self._request.conversation)
        return self._manager.plugin_conversation_port(self._plugin)

    @property
    def delivery(self):
        self._require("delivery", self._request.delivery)
        return self._manager.plugin_notification_port(self._plugin, capability="active")

    @property
    def storage(self):
        self._validate()
        return self._manager.plugin_storage_port(self._plugin)

    @property
    def llm(self):
        self._require("llm", self._request.llm)
        return self._manager.plugin_llm_port(self._plugin)

    @property
    def events(self):
        self._require("events", self._request.events)
        return self._manager.plugin_event_port(self._plugin)

    @property
    def artifacts(self):
        self._require("artifacts", self._request.artifacts)
        return self._manager.plugin_artifact_port(self._plugin)

    def safe_summary(self) -> dict[str, Any]:
        return self._request.safe_summary()

    def _require(self, name: str, declared: bool) -> None:
        self._validate()
        if not declared:
            raise PermissionError(
                f"active plugin resource was not declared: {self._plugin.key}:{name}"
            )

    def _validate(self) -> None:
        scope = self._plugin.generation_scope
        if scope is None or scope.closed:
            raise RuntimeError(f"plugin generation is no longer active: {self._plugin.key}")


class ActiveToolPort:
    def __init__(self, *, manager, plugin, allowed: set[str]) -> None:
        self._manager = manager
        self._plugin = plugin
        self._allowed = frozenset(allowed)

    @property
    def allowed(self) -> tuple[str, ...]:
        return tuple(sorted(self._allowed))

    async def call(self, name: str, arguments: dict[str, Any] | None = None):
        self._validate_generation()
        tool_name = str(name or "").strip()
        if tool_name not in self._allowed:
            raise PermissionError(
                f"active plugin tool is not allowlisted: {self._plugin.key}:{tool_name}"
            )

        from personal_agent.tools.execution_guard import tool_permission_category
        from personal_agent.tools.executor import execute_tool_call_result
        from personal_agent.tools.registry import tool_registry

        entry = tool_registry.get(tool_name)
        if entry is None:
            raise KeyError(f"active plugin tool is unavailable: {tool_name}")
        category = tool_permission_category(tool_name, entry)
        tags = {str(value).strip().lower() for value in (entry.tags or [])}
        if (
            entry.is_destructive
            or tool_name in _BLOCKED_TOOL_NAMES
            or category in _BLOCKED_TOOL_CATEGORIES
            or tags & _BLOCKED_TOOL_TAGS
        ):
            raise PermissionError(
                f"active plugins cannot execute destructive tool: {tool_name}"
            )
        if str(entry.approval_mode or "").strip().lower() == "deny":
            raise PermissionError(f"active plugin tool is disabled by policy: {tool_name}")

        agent = self._execution_context()
        return await execute_tool_call_result(
            {
                "id": f"active:{self._plugin.runtime_instance_id}:{uuid4().hex}",
                "name": tool_name,
                "input": dict(arguments or {}),
            },
            agent=agent,
            confirm=None,
        )

    def _execution_context(self):
        from personal_agent.security.evaluator import isolated_security_context

        return SimpleNamespace(
            _security_context=isolated_security_context("full-auto"),
            _hook_manager=self._manager.hook_manager,
            _hook_turn_id=f"active:{self._plugin.runtime_instance_id}",
            _hook_source=None,
            _hook_additional_contexts=[],
            _plugin_manager=None,
            _tool_bindings={},
            _tool_calls_this_turn=0,
            _max_tool_calls_per_turn=1,
            _destructive_calls_this_turn=0,
            _max_destructive_per_turn=0,
            _artifact_store=getattr(self._manager, "_artifact_store", None),
            _memory_session_key=f"plugin:{self._plugin.key}",
        )

    def _validate_generation(self) -> None:
        scope = self._plugin.generation_scope
        if scope is None or scope.closed:
            raise RuntimeError(f"plugin generation is no longer active: {self._plugin.key}")


class ActiveMCPPort:
    def __init__(
        self,
        *,
        tool_port: ActiveToolPort,
        request: ActiveResourceRequest,
    ) -> None:
        self._tool_port = tool_port
        self._request = request

    async def call(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
    ):
        server_name = str(server or "").strip()
        tool_name = str(tool or "").strip()
        allowed = self._request.mcp.get(server_name, ())
        if tool_name not in allowed:
            raise PermissionError(
                f"active plugin MCP tool is not allowlisted: {server_name}:{tool_name}"
            )
        return await self._tool_port.call(
            f"mcp__{server_name}__{tool_name}",
            arguments,
        )
