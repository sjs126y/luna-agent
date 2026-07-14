"""Codex MCP registration with a host-enforced tool policy."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from personal_agent.hooks import HookEvent, PreToolUseOutcome


class CodexBridgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = "codex"
    source_codex_home: Path = Field(default_factory=lambda: Path.home() / ".codex")
    runtime_codex_home: Path | None = None
    cwd: Path | None = None
    sandbox: Literal["read-only", "workspace-write"] = "workspace-write"
    connect_timeout_seconds: float = Field(default=60.0, gt=0)
    call_timeout_seconds: float = Field(default=1800.0, gt=0)


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

    ctx.register_mcp_server({
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

    ctx.register_hook(
        HookEvent.PRE_TOOL_USE,
        enforce_codex_policy,
        name="enforce-codex-session-policy",
        matcher=r"^mcp__codex__(?:codex|codex-reply)$",
        priority=10,
    )


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
