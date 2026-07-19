"""Execution lanes for slash commands handled by the conversation coordinator."""

from __future__ import annotations

from enum import StrEnum


class CommandExecutionPolicy(StrEnum):
    CONTROL = "control"
    SNAPSHOT = "snapshot"
    IMMEDIATE = "immediate"
    NEXT_TURN = "next_turn"
    BARRIER = "barrier"


_CONTROL = frozenset({"stop", "steer"})
_NEXT_TURN = frozenset({"mode"})
_BARRIER = frozenset({"new"})
_IMMEDIATE = frozenset({"deny"})
_SNAPSHOT = frozenset(
    {
        "activity",
        "agent-runs",
        "agents",
        "commands",
        "export",
        "help",
        "permissions",
        "protocol",
        "tool-runs",
        "tools",
        "usage",
    }
)


def command_execution_policy(text: str) -> CommandExecutionPolicy | None:
    """Return a lane for a slash command, or ``None`` for normal conversation."""

    parsed = parse_slash_command(text)
    if parsed is None:
        return None
    name, args = parsed
    if name in _CONTROL:
        return CommandExecutionPolicy.CONTROL
    if name in _NEXT_TURN:
        return CommandExecutionPolicy.NEXT_TURN
    if name in _BARRIER:
        return CommandExecutionPolicy.BARRIER
    if name in _IMMEDIATE:
        return CommandExecutionPolicy.IMMEDIATE
    if name == "session":
        action = args.partition(" ")[0].lower()
        if action in {"switch", "rename", "delete"}:
            return CommandExecutionPolicy.BARRIER
        return CommandExecutionPolicy.SNAPSHOT
    if name == "memory":
        action = args.partition(" ")[0].lower()
        if action == "delete":
            return CommandExecutionPolicy.BARRIER
        return CommandExecutionPolicy.SNAPSHOT
    if name in _SNAPSHOT:
        return CommandExecutionPolicy.SNAPSHOT
    # Plugin and skill commands may mutate or forward into an agent turn. Keep
    # them ordered behind the session's existing work until their handler says.
    return CommandExecutionPolicy.BARRIER


def parse_slash_command(text: str) -> tuple[str, str] | None:
    value = str(text or "").strip()
    if not value.startswith("/") or value == "/":
        return None
    body = value[1:].strip()
    if not body:
        return None
    name, _, args = body.partition(" ")
    return name.lower(), args.strip()
