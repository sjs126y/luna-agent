"""Minimal plugin example for Personal Agent."""

from __future__ import annotations

from personal_agent.plugins import CommandEntry

_GREETING = "hello"


def hello_command(args: str = "", **kwargs) -> str:
    target = args.strip() or "world"
    return f"{_GREETING}, {target}"


def hello_hook(value=None, **kwargs):
    return value if value is not None else "hello-hook"


def register(ctx) -> None:
    global _GREETING
    _GREETING = str(ctx.config.get("greeting", "hello"))
    ctx.register_skills("skills")
    ctx.register_command(CommandEntry(
        name="hello",
        description="Return a small greeting.",
        handler=hello_command,
        scope="both",
    ))
    ctx.register_hook("example_hello", hello_hook, priority=100)
