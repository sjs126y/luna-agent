"""Shared command and path policy for host shells and the Windows broker.

The policy is intentionally independent from process launching.  A caller
provides the effective platform, network flag, path declarations, and whether
the configured path restriction is enabled; the same checks then run in the
host tool and inside the broker before PowerShell is started.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Iterable

WHITELIST: dict[str, tuple[str | list[str], bool]] = {
    "ls": ("*", False), "dir": ("*", False), "cat": ("*", False),
    "type": ("*", False), "head": ("*", False), "tail": ("*", False),
    "wc": ("*", False), "find": ("*", False), "grep": ("*", False),
    "cp": ("*", False), "mv": ("*", False), "mkdir": ("*", False),
    "rmdir": ("*", False), "touch": ("*", False), "rm": ("*", False),
    "tree": ("*", False), "git": ("*", False), "python": ("*", False),
    "python3": ("*", False), "pip": ("*", True), "uv": ("*", True),
    "echo": ("*", False), "sed": ("*", False), "awk": ("*", False),
    "sort": ("*", False), "uniq": ("*", False), "cut": ("*", False),
    "tr": ("*", False), "diff": ("*", False), "whoami": ([], False),
    "pwd": ([], False), "date": ([], False), "env": ([], False),
    "uname": ([], False), "hostname": ([], False), "df": ("*", False),
    "du": ("*", False), "ps": ("*", False), "which": ("*", False),
    "where": ("*", False), "gcc": ("*", False), "g++": ("*", False),
    "make": ("*", False), "cargo": ("*", True), "go": ("*", False),
    "rustc": ("*", False), "curl": ("*", True), "wget": ("*", True),
    "npx": ("*", True), "npm": ("*", True),
}

_WINDOWS_WHITELIST: dict[str, tuple[str | list[str], bool]] = {
    "get-childitem": ("*", False), "get-content": ("*", False),
    "select-string": ("*", False), "get-location": ([], False),
    "get-date": ([], False), "get-process": ("*", False),
    "copy-item": ("*", False), "move-item": ("*", False),
    "new-item": ("*", False), "remove-item": ("*", False),
    "write-output": ("*", False), "sort-object": ("*", False),
    "compare-object": ("*", False), "invoke-webrequest": ("*", True),
    "invoke-restmethod": ("*", True),
}

_HARD_BLACKLIST: tuple[str, ...] = (
    r"\brm\s+-rf\s+/", r"\brm\s+-rf\s+/\*", r"\brm\s+-rf\s+~",
    r"\brm\s+-rf\s+\$HOME",
    r"\brm\s+-rf\s+/(etc|boot|bin|sbin|lib|lib64|sys|proc|dev)\b",
    r"\bdd\s+.*\bof=/dev/[sh]da", r"\bdd\s+.*\bof=\\\\.\\",
    r"\bdd\s+.*\bof=/dev/(null|zero|random)", r"\bmkfs\.",
    r"\bmkfs\s", r"\bmke2fs\b", r">\s*/dev/[sh]d[a-z]",
    r">\s*\\\\.\\[A-Z]", r":\(\)\s*\{", r"\)\(\)\s*\{",
    r"(?:^|[\s;&|])(?:sudo\s+)?(?:shutdown|reboot|halt|poweroff|init\s+[06])\b",
    r"\bchmod\s+777\s+/", r">\s*/etc/(passwd|shadow|sudoers|hosts)",
    r">\s*C:\\Windows\\(System32|SysWOW64)",
    r"\b(modprobe|sysctl|kldload)\b.*\b(-[a-z]*r\b|write\b)",
)

_DANGEROUS_PATTERNS: tuple[str, ...] = (
    r">\s*\\\\.\\", r">\s*/etc/", r">\s*C:\\Windows",
    r"\|.*sh\b", r"`[^`]+`", r"\$\([^)]+\)", r"\bsudo\b.*\brm\b",
    r"\bgit\s+push\s+--force",
    r"(?i)(?:^|[\s;])(?:invoke-expression|iex|start-process|new-object|add-type)\b",
    r"(?i)(?:^|[\s;])(?:set-executionpolicy|set-location\s+registry:)\b",
    r"(?i)\[system\.(?:io|reflection|management)\.",
    r"(?i)(?:^|[\s])\.(?:\\|/)", r"(?i)(?:^|[\s])&(?:\s|$)",
    r"(?i)-encodedcommand\b",
)

_PATH_ESCAPE_PATTERNS: tuple[str, ...] = (
    r"(?:^|\s)/(?:etc|var|tmp|home|root|proc|sys|dev|opt|usr|bin|sbin|boot)/",
    r"(?:^|\s)[A-Za-z]:[\\\\/](?:Windows|Program|Users|WINDOWS)",
    r"(?:^|\s)~(?:[/\s]|$)", r"(?:^|\s)\.\.(?:\s|$|/|\\)",
)


def effective_whitelist(*, is_windows: bool) -> dict[str, tuple[str | list[str], bool]]:
    return {**WHITELIST, **_WINDOWS_WHITELIST} if is_windows else WHITELIST


def check_command(
    cmd_line: str,
    *,
    declared_paths: Iterable[Path] = (),
    allow_network: bool = False,
    restrict_paths: bool = True,
    is_windows: bool = False,
    sandbox: object | None = None,
) -> str | None:
    """Return a user-facing policy error, or ``None`` when allowed."""
    cmd_stripped = str(cmd_line or "").strip()
    parts = cmd_stripped.split()
    if not parts:
        return "Error: empty command"
    cmd_lower = cmd_stripped.lower()
    for pattern in _HARD_BLACKLIST:
        if re.search(pattern, cmd_lower, re.IGNORECASE):
            return "Error: catastrophic command blocked by hard blacklist — this cannot be overridden"
    if any(token in cmd_stripped for token in ("&&", "||", "|", ";")):
        return "Error: command chaining (&& || | ;) is not allowed. Use one command per call."
    path_error = check_path_sandbox(
        cmd_stripped,
        declared_paths=declared_paths,
        restrict_paths=restrict_paths,
        sandbox=sandbox,
    )
    if path_error:
        return path_error
    base = parts[0].lower().replace("\\", "/").split("/")[-1]
    whitelist = effective_whitelist(is_windows=is_windows)
    if base not in whitelist:
        return (
            f"Error: command '{base}' is not in the allowed list. "
            f"Allowed commands: {', '.join(sorted(whitelist.keys()))}"
        )
    _, needs_network = whitelist[base]
    if needs_network and not allow_network:
        return (
            f"Error: network access not allowed (blocked '{base}'). "
            "Set bash_allow_network: true in config.yaml to enable."
        )
    for pattern in _DANGEROUS_PATTERNS:
        if re.search(pattern, cmd_stripped.lower(), re.IGNORECASE):
            return "Error: dangerous pattern detected"
    return None


def check_path_sandbox(
    cmd_line: str,
    *,
    declared_paths: Iterable[Path] = (),
    restrict_paths: bool = True,
    sandbox: object | None = None,
) -> str | None:
    if sandbox is None:
        from luna_agent.tools.sandbox import get_sandbox

        sandbox = get_sandbox()
    blocked = getattr(sandbox, "blocked", ())
    for pattern in blocked:
        regex = glob_pattern_to_regex(pattern)
        if re.search(regex, cmd_line, re.IGNORECASE):
            return (
                f"Error: sandbox blocked — '{pattern}' matches protected files. "
                "Config and credential files are never readable via bash."
            )
    if not restrict_paths:
        return None
    declared = tuple(declared_paths)
    if declared:
        return None
    cmd_norm = cmd_line.replace("\\", "/")
    for root in getattr(sandbox, "roots", ()):
        root_text = str(root).replace("\\", "/")
        if re.search(rf"(?:^|\s){re.escape(root_text)}(?:/|$)", cmd_norm):
            return None
    for pattern in _PATH_ESCAPE_PATTERNS:
        if re.search(pattern, cmd_line, re.IGNORECASE):
            return (
                "Error: path sandbox blocked — absolute system path or parent "
                "traversal detected. Use only relative paths within the working "
                "directory, or absolute paths under configured sandbox roots."
            )
    return None


def glob_pattern_to_regex(glob_pat: str) -> str:
    pat = str(glob_pat).strip()
    if pat.startswith("**/"):
        pat = pat[3:]
    if pat.endswith("/**"):
        pat = pat[:-3] + "/"
    return "[^/\\\\\\s]*".join(re.escape(part) for part in pat.split("*"))


def command_network_target(command: str) -> str | None:
    """Return a stable audit target for a network-enabled command."""
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return None
    spec = effective_whitelist(is_windows=False).get(parts[0].lower())
    if spec is None or not spec[1]:
        return None
    return f"command:{parts[0].lower()}"

