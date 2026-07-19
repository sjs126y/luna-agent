"""Context7-backed developer documentation workflows."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from luna_agent_plugin_sdk import CommandEntry


class DeveloperDocsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = "npx"
    package: str = "@upstash/context7-mcp"
    connect_timeout_seconds: float = Field(default=60.0, gt=0)
    call_timeout_seconds: float = Field(default=120.0, gt=0)


def register(ctx) -> None:
    config = ctx.parse_config(DeveloperDocsConfig)
    ctx.register.mcp_server({
        "name": "context7",
        "transport": "stdio",
        "command": config.command,
        "args": ["-y", config.package],
        "connect_timeout_seconds": config.connect_timeout_seconds,
        "call_timeout_seconds": config.call_timeout_seconds,
        "allow_network": True,
        "max_tools": 8,
    })
    ctx.register.skills("skills")
    ctx.register.command(CommandEntry(
        name="developer-docs-status",
        description="Show Developer Docs and Context7 configuration.",
        handler=lambda args="", **kwargs: (
            "Developer Docs\n"
            "- MCP: context7\n"
            f"- package: {config.package}\n"
            "- workflows: library-docs, upgrade-library, compare-library-api"
        ),
        scope="both",
    ))
