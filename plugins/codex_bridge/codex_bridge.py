"""Codex MCP registration with a host-enforced tool policy."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from luna_agent_plugin_sdk import ActiveResourceRequest, HookEvent, PreToolUseOutcome, ToolEntry

from .development import CodexDevelopmentRuntime


class ActiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    sessions: list[str] = Field(default_factory=list)


class CodexBridgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = "codex"
    source_codex_home: Path = Field(default_factory=lambda: Path.home() / ".codex")
    runtime_codex_home: Path | None = None
    cwd: Path | None = None
    sandbox: Literal["read-only", "workspace-write"] = "workspace-write"
    connect_timeout_seconds: float = Field(default=60.0, gt=0)
    call_timeout_seconds: float = Field(default=1800.0, gt=0)
    development_root: Path | None = None
    development_spec_path: Path | None = None
    development_spec_revision: str = "1"
    approval_policy: Literal["on-request", "never"] = "on-request"
    approvals_reviewer: Literal["user", "auto_review"] = "user"
    app_server_timeout_seconds: float = Field(default=60.0, gt=0)
    event_retention: int = Field(default=1000, ge=100, le=10000)
    active: ActiveConfig = Field(default_factory=ActiveConfig)


def register(ctx) -> None:
    config = ctx.parse_config(CodexBridgeConfig)
    command = str(config.command).strip()
    resolved_command = shutil.which(command) if command else None
    if resolved_command is None:
        raise ValueError(f"Codex executable was not found: {command or '<empty>'}")

    writable_roots = [Path(item).expanduser().resolve() for item in ctx.settings.sandbox_roots]
    cwd = (config.cwd or (writable_roots[0] if writable_roots else Path.cwd()))
    cwd = Path(cwd).expanduser().resolve()
    if not any(_is_within(cwd, root) for root in writable_roots):
        raise ValueError("Codex Bridge cwd must be within sandbox.roots")

    source_codex_home = Path(config.source_codex_home).expanduser().resolve()
    runtime_codex_home = Path(
        config.runtime_codex_home or (ctx.settings.agent_data_dir / "codex-bridge")
    ).expanduser().resolve()
    if not any(_is_within(runtime_codex_home, root) for root in writable_roots):
        raise ValueError("Codex Bridge runtime_codex_home must be within sandbox.roots")
    _prepare_runtime_home(source_codex_home, runtime_codex_home)
    development_root = Path(
        config.development_root
        or (Path.home() / ".local" / "share" / "luna-agent" / "plugin-workspaces")
    ).expanduser().resolve()
    development_spec_path = Path(
        config.development_spec_path or (Path(__file__).resolve().parents[2] / "docs" / "plugin-development.md")
    ).expanduser().resolve()
    if _is_within(development_root, cwd):
        raise ValueError("Codex Bridge development_root must be outside the host workspace")
    if development_root in {Path(development_root.anchor), Path.home().resolve()}:
        raise ValueError("Codex Bridge development_root is too broad")
    development_root.mkdir(parents=True, exist_ok=True)
    runtime = CodexDevelopmentRuntime(config=config.model_copy(update={
        "runtime_codex_home": runtime_codex_home,
        "development_root": development_root,
        "development_spec_path": development_spec_path,
    }), ctx=ctx)

    ctx.register.mcp_server({
        "name": "codex",
        "transport": "stdio",
        "command": sys.executable,
        "args": [
            str(ctx.resolve_path("stdio_filter.py")),
            str(resolved_command),
            "mcp-server",
        ],
        "env": {"CODEX_HOME": str(runtime_codex_home)},
        "connect_timeout_seconds": config.connect_timeout_seconds,
        "call_timeout_seconds": config.call_timeout_seconds,
        "allow_network": True,
        "max_tools": 8,
    })

    def enforce_codex_policy(envelope):
        tool_name = str(envelope.payload.get("tool_name") or "")
        if tool_name != "mcp__codex__codex":
            return None
        original = dict(envelope.payload.get("tool_input") or {})
        updated = {
            key: original[key]
            for key in (
                "prompt",
                "model",
                "base-instructions",
                "developer-instructions",
                "compact-prompt",
            )
            if key in original
        }
        updated.update({
            "cwd": str(cwd),
            "sandbox": config.sandbox,
            "approval-policy": "never",
            "config": {"mcp_servers": {}},
        })
        return PreToolUseOutcome(updated_input=updated)

    ctx.register.hook(
        HookEvent.PRE_TOOL_USE,
        enforce_codex_policy,
        name="enforce-codex-session-policy",
        matcher=r"^mcp__codex__(?:codex|codex-reply)$",
        priority=10,
    )

    _register_development_tools(ctx, runtime)
    ctx.register.active(
        run=runtime.run,
        resources=ActiveResourceRequest(conversation=True),
        restart_policy="on_failure",
        startup_timeout=20,
        shutdown_timeout=20,
    )


def _register_development_tools(ctx, runtime: CodexDevelopmentRuntime) -> None:
    async def create_handler(plugin_id: str, description: str, brief: str = ""):
        return await runtime.create(plugin_id, description, brief)

    async def message_handler(plugin_id: str, text: str):
        return await runtime.message(plugin_id, text)

    async def list_handler():
        return runtime.list_sessions()

    async def status_handler(plugin_id: str):
        return runtime.status(plugin_id)

    async def events_handler(
        plugin_id: str,
        limit: int = 20,
        offset: int = 0,
        order: str = "desc",
        event_types: list[str] | None = None,
        detail: str = "summary",
    ):
        return runtime.events(
            plugin_id,
            limit=limit,
            offset=offset,
            order=order,
            event_types=event_types,
            detail=detail,
        )

    async def cancel_handler(plugin_id: str):
        return await runtime.cancel(plugin_id)

    async def approval_list_handler(plugin_id: str = ""):
        return runtime.approvals(plugin_id)

    async def approval_decide_handler(request_id: str, decision: str):
        return await runtime.decide_approval(request_id, decision)

    ctx.register.tool(ToolEntry(
        name="plugin_dev_create",
        description=(
            "Create an external Luna plugin development workspace and one persistent Codex thread. "
            "The user only needs to provide the plugin's functional goal; the generated development "
            "contract supplies SDK, package, security, testing, and lifecycle requirements."
        ),
        schema={"type": "object", "properties": {
            "plugin_id": {"type": "string"}, "description": {"type": "string"}, "brief": {"type": "string"},
        }, "required": ["plugin_id", "description"], "additionalProperties": False},
        handler=create_handler,
        toolset="plugin", permission_category="write", approval_mode="cached", risk_level="medium",
        tags=["plugin", "codex", "development"], idempotent=True, is_parallel_safe=False,
    ))
    ctx.register.tool(ToolEntry(
        name="plugin_dev_message",
        description=(
            "Send a functional requirement or follow-up to the Codex thread assigned to one plugin; "
            "the first turn automatically loads the generated Luna development contract. Returns "
            "immediately when accepted or queued."
        ),
        schema={"type": "object", "properties": {"plugin_id": {"type": "string"}, "text": {"type": "string"}}, "required": ["plugin_id", "text"], "additionalProperties": False},
        handler=message_handler,
        toolset="plugin", permission_category="write", approval_mode="cached", risk_level="medium",
        tags=["plugin", "codex", "message", "async"], idempotent=False, is_parallel_safe=False,
    ))
    ctx.register.tool(ToolEntry(
        name="plugin_dev_list",
        description="List persistent Codex plugin development sessions and their current status.",
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=list_handler, toolset="plugin", permission_category="read", approval_mode="auto",
        tags=["plugin", "codex", "sessions"], idempotent=True,
    ))
    ctx.register.tool(ToolEntry(
        name="plugin_dev_status",
        description="Show one plugin development session status, thread, workspace, and last result.",
        schema={"type": "object", "properties": {"plugin_id": {"type": "string"}}, "required": ["plugin_id"], "additionalProperties": False},
        handler=status_handler, toolset="plugin", permission_category="read", approval_mode="auto",
        tags=["plugin", "codex", "status"], idempotent=True,
    ))
    ctx.register.tool(ToolEntry(
        name="plugin_dev_events",
        description=(
            "Page through Codex development events for one plugin. Choose up to 200 events, "
            "newest or oldest first, filter event types, and request summary or full protocol metadata."
        ),
        schema={"type": "object", "properties": {
            "plugin_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "order": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
            "event_types": {"type": "array", "items": {"type": "string"}},
            "detail": {"type": "string", "enum": ["summary", "full"], "default": "summary"},
        }, "required": ["plugin_id"], "additionalProperties": False},
        handler=events_handler, toolset="plugin", permission_category="read", approval_mode="auto",
        tags=["plugin", "codex", "events"], idempotent=True,
    ))
    ctx.register.tool(ToolEntry(
        name="plugin_dev_cancel",
        description="Interrupt the active Codex turn for one plugin and mark the session cancelled.",
        schema={"type": "object", "properties": {"plugin_id": {"type": "string"}}, "required": ["plugin_id"], "additionalProperties": False},
        handler=cancel_handler, toolset="plugin", permission_category="write", approval_mode="cached", risk_level="medium",
        tags=["plugin", "codex", "cancel"], idempotent=False, is_parallel_safe=False,
    ))
    ctx.register.tool(ToolEntry(
        name="codex_approval_list",
        description="List pending Codex App Server approval requests; Codex remains responsible for its own approval policy.",
        schema={"type": "object", "properties": {"plugin_id": {"type": "string"}}, "additionalProperties": False},
        handler=approval_list_handler, toolset="plugin", permission_category="read", approval_mode="auto",
        tags=["codex", "approval", "events"], idempotent=True,
    ))
    ctx.register.tool(ToolEntry(
        name="codex_approval_decide",
        description="Allow once or deny a pending Codex approval request after Luna has obtained the user's decision.",
        schema={"type": "object", "properties": {"request_id": {"type": "string"}, "decision": {"type": "string", "enum": ["allow_once", "deny"]}}, "required": ["request_id", "decision"], "additionalProperties": False},
        handler=approval_decide_handler, toolset="plugin", permission_category="write", approval_mode="cached", risk_level="high",
        tags=["codex", "approval", "decision"], idempotent=False, is_parallel_safe=False,
    ))


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _prepare_runtime_home(source: Path, runtime: Path) -> None:
    source_auth = source / "auth.json"
    if not source_auth.is_file():
        raise ValueError(f"Codex auth file was not found: {source_auth}")
    runtime.mkdir(parents=True, exist_ok=True)
    runtime_auth = runtime / "auth.json"
    if not runtime_auth.exists():
        shutil.copyfile(source_auth, runtime_auth)
        runtime_auth.chmod(0o600)
    source_config = source / "config.toml"
    if source_config.is_file():
        runtime_config = runtime / "config.toml"
        shutil.copyfile(source_config, runtime_config)
        runtime_config.chmod(0o600)
