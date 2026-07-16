"""GitHub MCP registration with repository and write-operation policy."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from personal_agent.hooks import HookEvent, PreToolUseOutcome
from personal_agent.plugins import CommandEntry


class GitHubAssistantConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = "https://api.githubcopilot.com/mcp/"
    auth_header_env: str = "GITHUB_MCP_AUTH"
    repositories: list[str] = Field(default_factory=list)
    write_enabled: bool = False
    connect_timeout_seconds: float = Field(default=60.0, gt=0)
    call_timeout_seconds: float = Field(default=120.0, gt=0)


_WRITE_TOOL = re.compile(
    r"(?:^|__)(?:add|create|delete|fork|merge|push|remove|request|rerun|submit|update)_",
    re.IGNORECASE,
)


def register(ctx) -> None:
    config = ctx.parse_config(GitHubAssistantConfig)
    repositories = {_normalize_repo(value) for value in config.repositories if _normalize_repo(value)}

    ctx.register_mcp_server({
        "name": "github",
        "transport": "streamable_http",
        "url": config.url,
        "headers_env": {"Authorization": config.auth_header_env},
        "connect_timeout_seconds": config.connect_timeout_seconds,
        "call_timeout_seconds": config.call_timeout_seconds,
        "allow_network": True,
        "max_tools": 100,
    })
    ctx.register_skills("skills")

    def enforce_github_policy(envelope):
        tool_name = str(envelope.payload.get("tool_name") or "")
        tool_input = dict(envelope.payload.get("tool_input") or {})
        if not config.write_enabled and _is_write_tool(tool_name):
            return PreToolUseOutcome.block(
                "GitHub Assistant write operations are disabled by plugin configuration"
            )
        requested = _repo_from_input(tool_input)
        if repositories and requested and requested not in repositories:
            return PreToolUseOutcome.block(
                f"GitHub repository is outside the plugin allowlist: {requested}"
            )
        return None

    ctx.register_hook(
        HookEvent.PRE_TOOL_USE,
        enforce_github_policy,
        name="enforce-github-policy",
        matcher=r"^mcp__github__.+$",
        priority=10,
    )
    ctx.register_command(CommandEntry(
        name="github-status",
        description="Show GitHub Assistant configuration and MCP runtime status.",
        handler=lambda args="", **kwargs: _status(config, repositories, kwargs),
        scope="both",
    ))


def _is_write_tool(tool_name: str) -> bool:
    short = tool_name.rsplit("__", 1)[-1]
    lowered = short.lower()
    return lowered.endswith("_write") or bool(_WRITE_TOOL.search(short)) or lowered in {
        "add_comment_to_pending_review",
        "create_pull_request_review",
        "dismiss_notification",
        "mark_all_notifications_read",
    }


def _repo_from_input(value: dict) -> str:
    owner = str(value.get("owner") or "").strip()
    repo = str(value.get("repo") or value.get("repository") or "").strip()
    if owner and repo:
        return _normalize_repo(f"{owner}/{repo}")
    if "/" in repo:
        return _normalize_repo(repo)
    return ""


def _normalize_repo(value: str) -> str:
    return str(value or "").strip().strip("/").lower()


def _status(config, repositories: set[str], kwargs: dict) -> str:
    state = _server_state(kwargs, "github")
    allowlist = ", ".join(sorted(repositories)) if repositories else "all repositories"
    return (
        "GitHub Assistant\n"
        f"- MCP: {state}\n"
        f"- repositories: {allowlist}\n"
        f"- write operations: {'enabled' if config.write_enabled else 'disabled'}"
    )


def _server_state(kwargs: dict, name: str) -> str:
    runtime = kwargs.get("runtime")
    app_runtime = getattr(runtime, "app_runtime", None)
    manager = getattr(app_runtime, "mcp_manager", None)
    gateway = kwargs.get("gateway")
    manager = manager or getattr(gateway, "_mcp_manager", None)
    if manager is None or not hasattr(manager, "health_snapshot"):
        return "configured (runtime status unavailable)"
    for server in manager.health_snapshot().get("servers", []):
        if str(server.get("name") or "") == name:
            return str(server.get("state") or "unknown")
    return "not registered"
