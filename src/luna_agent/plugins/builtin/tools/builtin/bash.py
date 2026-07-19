"""Safe shell command execution — whitelist + sandbox.

Layered defense:
  1. Command whitelist — unknown commands blocked
  2. Argument-level dangerous pattern detection
  3. Network isolation (curl/wget/pip blocked unless config allows)
  4. Working directory restricted to data dir
  5. Timeout (default 30s)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import shlex
import subprocess
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from luna_agent.tools.entry import ToolEntry
from luna_agent.tools.registry import tool_registry

logger = logging.getLogger(__name__)

# ── sandbox config (set at startup) ──────────────────

_work_dir: Path = Path("./data").resolve()
_allow_network: bool = False
_restrict_paths: bool = True
_process_backend: str = "auto"
_MAX_OUTPUT = 4000
_MAX_CAPTURE_BYTES = 64_000


def set_work_dir(path: Path) -> None:
    global _work_dir
    _work_dir = path.resolve()


def set_restrict_paths(restrict: bool) -> None:
    global _restrict_paths
    _restrict_paths = restrict


def set_allow_network(allowed: bool) -> None:
    global _allow_network
    _allow_network = allowed


def set_process_backend(backend: str) -> None:
    global _process_backend
    from luna_agent.tools.process_sandbox import normalize_process_backend

    _process_backend = normalize_process_backend(backend)


# ── command whitelist ─────────────────────────────────
# Format: command_name → (arg_patterns, needs_network)
# arg_patterns: "*" = any args allowed; ["-n", "-l"] = only these flags

WHITELIST: dict[str, tuple[str | list[str], bool]] = {
    # File ops
    "ls":     ("*", False),   "dir":    ("*", False),
    "cat":    ("*", False),   "type":   ("*", False),
    "head":   ("*", False),   "tail":   ("*", False),
    "wc":     ("*", False),   "find":   ("*", False),
    "grep":   ("*", False),   "cp":     ("*", False),
    "mv":     ("*", False),   "mkdir":  ("*", False),
    "rmdir":  ("*", False),   "touch":  ("*", False),
    "rm":     ("*", False),   "tree":   ("*", False),
    # Git
    "git":    ("*", False),
    # Python
    "python": ("*", False),   "python3": ("*", False),
    "pip":    ("*", True),    "uv":      ("*", True),
    # Text processing
    "echo":   ("*", False),   "sed":    ("*", False),
    "awk":    ("*", False),   "sort":   ("*", False),
    "uniq":   ("*", False),   "cut":    ("*", False),
    "tr":     ("*", False),   "diff":   ("*", False),
    # System info (no destructive args)
    "whoami":  ([], False),   "pwd":    ([], False),
    "date":    ([], False),   "env":    ([], False),
    "uname":   ([], False),   "hostname": ([], False),
    "df":     ("*", False),   "du":     ("*", False),
    "ps":     ("*", False),   "which":  ("*", False),
    "where":  ("*", False),
    # Compilers / build
    "gcc":    ("*", False),   "g++":   ("*", False),
    "make":   ("*", False),   "cargo": ("*", True),
    "go":     ("*", False),   "rustc": ("*", False),
    # Network tools (only if _allow_network)
    "curl":   ("*", True),    "wget":  ("*", True),
    "npx":    ("*", True),    "npm":   ("*", True),
}

# Windows command aliases
_WINDOWS_ALIASES: dict[str, str] = {
    "dir": "dir", "type": "type", "findstr": "findstr",
    "where": "where",
}

# ── Hard blacklist — catastrophic commands, NEVER allowed ──
# These are checked BEFORE the whitelist and cannot be overridden.
# Tool approval never bypasses these hard checks.

_HARD_BLACKLIST: list[str] = [
    # Filesystem destruction (root paths)
    r'\brm\s+-rf\s+/', r'\brm\s+-rf\s+/\*',
    r'\brm\s+-rf\s+~', r'\brm\s+-rf\s+\$HOME',
    r'\brm\s+-rf\s+/(etc|boot|bin|sbin|lib|lib64|sys|proc|dev)\b',
    # Block device writes
    r'\bdd\s+.*\bof=/dev/[sh]da', r'\bdd\s+.*\bof=\\\\.\\',
    r'\bdd\s+.*\bof=/dev/(null|zero|random)',
    # Format / mkfs
    r'\bmkfs\.', r'\bmkfs\s', r'\bmke2fs\b',
    # Raw disk writes
    r'>\s*/dev/[sh]d[a-z]', r'>\s*\\\\.\\[A-Z]',
    # Fork bomb
    r':\(\)\s*\{', r'\)\(\)\s*\{',
    # System shutdown (anchored to command start, not args)
    r'(?:^|[\s;&|])(?:sudo\s+)?(?:shutdown|reboot|halt|poweroff|init\s+[06])\b',
    # chmod 777 on system dirs
    r'\bchmod\s+777\s+/',
    # Write to system config
    r'>\s*/etc/(passwd|shadow|sudoers|hosts)',
    r'>\s*C:\\Windows\\(System32|SysWOW64)',
    # Kernel module / sysctl tampering
    r'\b(modprobe|sysctl|kldload)\b.*\b(-[a-z]*r\b|write\b)',
]


# Dangerous argument patterns — blocked regardless of whitelist
_DANGEROUS_PATTERNS: list[str] = [
    r'>\s*\\\\.\\',                              # write to raw devices (Windows)
    r'>\s*/etc/', r'>\s*C:\\Windows',            # system config overwrite
    r'\|.*sh\b', r'`[^`]+`',                    # pipe to shell / backtick injection
    r'\$\([^)]+\)',                               # command substitution
    r'\bsudo\b.*\brm\b',                         # sudo rm (any target)
    r'\bgit\s+push\s+--force',                   # force push (potentially destructive)
]


def _check_command(
    cmd_line: str,
    *,
    declared_paths: Iterable[Path] = (),
) -> str | None:
    """Validate command against hard blacklist → whitelist → patterns.

    Layer order:
      0. Hard blacklist — catastrophic, unconditional, NEVER bypassed
      1. Command chaining detection
      2. Whitelist check
      3. Network isolation
      4. Dangerous pattern detection
    """
    cmd_stripped = cmd_line.strip()

    # Extract base command (first word, handling quotes)
    parts = cmd_stripped.split()
    if not parts:
        return "Error: empty command"

    # ── 0. Hard blacklist (UNCONDITIONAL) ──
    cmd_lower = cmd_stripped.lower()
    for pattern in _HARD_BLACKLIST:
        if re.search(pattern, cmd_lower, re.IGNORECASE):
            return f"Error: catastrophic command blocked by hard blacklist — this cannot be overridden"

    # ── 1. Block command chaining ──
    _CHAIN_TOKENS = ("&&", "||", "|", ";")
    if any(tok in cmd_stripped for tok in _CHAIN_TOKENS):
        return "Error: command chaining (&& || | ;) is not allowed. Use one command per call."

    # ── 1.5. Path sandbox — no absolute paths, no traversal ──
    path_error = _check_path_sandbox(cmd_stripped, declared_paths=declared_paths)
    if path_error:
        return path_error

    base = parts[0].lower().replace("\\", "/").split("/")[-1]  # strip path

    # ── 2. Whitelist check ──
    if base not in WHITELIST:
        return (
            f"Error: command '{base}' is not in the allowed list. "
            f"Allowed commands: {', '.join(sorted(WHITELIST.keys()))}"
        )

    _, needs_network = WHITELIST[base]
    if needs_network and not _allow_network:
        return (
            f"Error: network access not allowed (blocked '{base}'). "
            f"Set bash_allow_network: true in config.yaml to enable."
        )

    # Check dangerous patterns (case-insensitive matching)
    cmd_normalized = cmd_stripped.lower()
    for pattern in _DANGEROUS_PATTERNS:
        if re.search(pattern, cmd_normalized, re.IGNORECASE):
            return f"Error: dangerous pattern detected"

    return None


# ── path sandbox ─────────────────────────────────────

# System-level escape patterns — paths that should never be accessible
# These go beyond sandbox roots: /etc, C:\Windows, ~, .. are dangerous
# regardless of configured roots.
_PATH_ESCAPE_PATTERNS: list[str] = [
    r'(?:^|\s)/(?:etc|var|tmp|home|root|proc|sys|dev|opt|usr|bin|sbin|boot)/',  # Unix system paths
    r'(?:^|\s)[A-Za-z]:[\\\\/](?:Windows|Program|Users|WINDOWS)',  # Windows system paths
    r'(?:^|\s)~(?:[/\s]|$)',     # ~/ home dir or bare ~
    r'(?:^|\s)\.\.(?:\s|$|/|\\)',  # parent dir traversal
]


def _glob_pattern_to_regex(glob_pat: str) -> str:
    """Convert a sandbox blocked glob (e.g. '**/.env') to a regex for command scanning.

    Examples:
      **/.env          -> \\.env
      **/.env.*        -> \\.env\\.[^/\\s]*
      **/.git/**       -> \\.git/
      **/id_rsa*       -> id_rsa[^/\\s]*
      **/data/auth/**  -> data/auth/
    """
    pat = glob_pat.strip()
    # Strip leading **/
    if pat.startswith("**/"):
        pat = pat[3:]
    # Strip trailing /**
    if pat.endswith("/**"):
        pat = pat[:-3] + "/"
    # Split on * wildcard, escape literal parts, rejoin with wildcard
    parts = pat.split("*")
    escaped = [re.escape(p) for p in parts]
    return "[^/\\\\\\s]*".join(escaped)


def _check_path_sandbox(
    cmd_line: str,
    *,
    declared_paths: Iterable[Path] = (),
) -> str | None:
    """Block commands that access files outside the sandbox.

    Uses the unified sandbox for blocked patterns and roots.
    Additionally blocks system-level escape patterns when _restrict_paths is true.
    """
    from luna_agent.tools.sandbox import get_sandbox
    sandbox = get_sandbox()

    # ── 1. Blocked patterns (from sandbox) — NEVER allowed ──
    for pattern in sandbox.blocked:
        regex = _glob_pattern_to_regex(pattern)
        if re.search(regex, cmd_line, re.IGNORECASE):
            return (
                f"Error: sandbox blocked — '{pattern}' matches "
                f"protected files. Config and credential files are never readable via bash."
            )

    # ── 2. Restrict paths off? allow everything except blocked ──
    if not _restrict_paths:
        return None

    # Explicit declarations are enforced by resource approval and bwrap mounts.
    # The command scanner remains a useful early check, not the security boundary.
    if tuple(declared_paths):
        return None

    # ── 3. Check if command references a path under a sandbox root ──
    cmd_norm = cmd_line.replace("\\", "/")
    for root in sandbox.roots:
        rs = str(root).replace("\\", "/")
        # Match as full path component — avoids Desktop matching DesktopProjects
        escaped_root = re.escape(rs)
        if re.search(rf'(?:^|\s){escaped_root}(?:/|$)', cmd_norm):
            return None  # path is under a configured root, allow

    # ── 4. Escape patterns — system paths that bypass root check ──
    for pattern in _PATH_ESCAPE_PATTERNS:
        if re.search(pattern, cmd_line, re.IGNORECASE):
            return (
                f"Error: path sandbox blocked — absolute system path or "
                f"parent traversal detected. Use only relative paths within "
                f"the working directory, or absolute paths under configured "
                f"sandbox roots."
            )

    return None


def _subprocess_group_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except Exception:
        proc.kill()


async def spawn_command(
    command: str,
    *,
    cwd: Path,
    read_paths: Iterable[Path] = (),
    write_paths: Iterable[Path] = (),
    stdout,
    stderr,
) -> asyncio.subprocess.Process:
    from luna_agent.tools.env_filter import filter_env
    from luna_agent.tools.process_sandbox import BASH_STRICT_POLICY, build_process_launch

    readable = tuple(Path(path).resolve() for path in read_paths)
    writable = tuple(Path(path).resolve() for path in write_paths)
    masks = _collect_blocked_mounts((cwd, *readable, *writable))
    launch = build_process_launch(
        command,
        cwd=cwd,
        readable_roots=readable,
        writable_roots=(cwd, *writable),
        masked_paths=masks,
        allow_network=_allow_network,
        requested_backend=_process_backend,
        policy=BASH_STRICT_POLICY,
    )
    kwargs = {
        "stdout": stdout,
        "stderr": stderr,
        "env": filter_env(),
        **_subprocess_group_kwargs(),
    }
    if launch.backend == "unavailable":
        raise RuntimeError(launch.warning)
    if launch.backend == "bwrap":
        return await asyncio.create_subprocess_exec(*launch.argv, **kwargs)
    return await asyncio.create_subprocess_shell(
        command,
        cwd=str(launch.cwd),
        **kwargs,
    )


# ── handler ──────────────────────────────────────────

async def _bash(
    command: str,
    timeout: int = 30,
    cwd: str = "",
    read_paths: list[str] | None = None,
    write_paths: list[str] | None = None,
) -> str:
    paths, path_error = _resolve_execution_paths(
        cwd=cwd,
        read_paths=read_paths,
        write_paths=write_paths,
    )
    if path_error:
        return path_error
    work_dir, readable, writable = paths
    error = _check_command(command, declared_paths=(work_dir, *readable, *writable))
    if error:
        return error

    started = time.monotonic()
    proc = None
    stdout_task = None
    stderr_task = None
    wait_task = None
    try:
        proc = await spawn_command(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
            read_paths=readable,
            write_paths=writable,
        )

        timeout = min(max(int(timeout or 30), 1), 60)
        stdout_task = asyncio.create_task(_drain_output(proc.stdout))
        stderr_task = asyncio.create_task(_drain_output(proc.stderr))
        wait_task = asyncio.create_task(proc.wait())
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await _kill_process_tree(proc)
                await wait_task
                (stdout, stdout_total), (stderr, stderr_total) = await asyncio.gather(
                    stdout_task,
                    stderr_task,
                )
                result = _format_command_result(
                    exit_code=proc.returncode,
                    duration=time.monotonic() - started,
                    stdout=stdout,
                    stderr=stderr,
                    stdout_total_bytes=stdout_total,
                    stderr_total_bytes=stderr_total,
                    timed_out=True,
                )
                return result
            try:
                await asyncio.wait_for(
                    asyncio.shield(wait_task),
                    timeout=min(1.0, remaining),
                )
                break
            except asyncio.TimeoutError:
                from luna_agent.tools.executor import is_interrupted
                if is_interrupted():
                    await _kill_process_tree(proc)
                    await wait_task
                    (stdout, stdout_total), (stderr, stderr_total) = await asyncio.gather(
                        stdout_task,
                        stderr_task,
                    )
                    result = _format_command_result(
                        exit_code=proc.returncode,
                        duration=time.monotonic() - started,
                        stdout=stdout,
                        stderr=stderr,
                        stdout_total_bytes=stdout_total,
                        stderr_total_bytes=stderr_total,
                        interrupted=True,
                    )
                    return result
        (stdout, stdout_total), (stderr, stderr_total) = await asyncio.gather(
            stdout_task,
            stderr_task,
        )
        result = _format_command_result(
            exit_code=proc.returncode,
            duration=time.monotonic() - started,
            stdout=stdout,
            stderr=stderr,
            stdout_total_bytes=stdout_total,
            stderr_total_bytes=stderr_total,
        )
        return result
    except Exception as e:
        if proc is not None and proc.returncode is None:
            await _kill_process_tree(proc)
        pending = [task for task in (stdout_task, stderr_task, wait_task) if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return f"Error: {e}"


async def _drain_output(reader) -> tuple[bytes, int]:
    if reader is None:
        return b"", 0
    captured = bytearray()
    total_bytes = 0
    while True:
        chunk = await reader.read(8192)
        if not chunk:
            break
        total_bytes += len(chunk)
        remaining = _MAX_CAPTURE_BYTES - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
    return bytes(captured), total_bytes


def _format_command_result(
    *,
    exit_code: int | None,
    duration: float,
    stdout: bytes,
    stderr: bytes,
    stdout_total_bytes: int | None = None,
    stderr_total_bytes: int | None = None,
    timed_out: bool = False,
    interrupted: bool = False,
) -> str:
    out, out_truncated = _decode_and_truncate(stdout, total_bytes=stdout_total_bytes)
    err, err_truncated = _decode_and_truncate(stderr, total_bytes=stderr_total_bytes)
    status = "timed out" if timed_out else "interrupted" if interrupted else "finished"
    lines = [
        f"Command {status}",
        f"exit_code: {exit_code if exit_code is not None else '-'}",
        f"duration: {duration:.2f}s",
        "stdout:",
        out or "(empty)",
        "stderr:",
        err or "(empty)",
    ]
    if out_truncated or err_truncated:
        lines.append("truncated: true")
    if timed_out:
        lines.append("hint: use process_start for long-running commands.")
    return "\n".join(lines)


def _decode_and_truncate(data: bytes, *, total_bytes: int | None = None) -> tuple[str, bool]:
    text = data.decode("utf-8", errors="replace").strip()
    total = max(len(data), int(total_bytes or 0))
    if len(text) <= _MAX_OUTPUT and total <= len(data):
        return text, False
    visible = text[:_MAX_OUTPUT]
    omitted = max(0, total - len(visible.encode("utf-8")))
    return visible + f"\n...({omitted} more bytes)", True


def _precheck(input_: dict) -> str | None:
    command = input_.get("command", "")
    paths, error = _resolve_execution_paths(
        cwd=input_.get("cwd", ""),
        read_paths=input_.get("read_paths"),
        write_paths=input_.get("write_paths"),
    )
    if error:
        return error
    work_dir, readable, writable = paths
    return (
        _check_command(command, declared_paths=(work_dir, *readable, *writable))
        if command
        else None
    )


def resource_requirements(input_: dict) -> list:
    """Describe the shell's declared filesystem and network resources."""
    from luna_agent.security.models import ResourceRequirement

    paths, error = _resolve_execution_paths(
        cwd=input_.get("cwd", ""),
        read_paths=input_.get("read_paths"),
        write_paths=input_.get("write_paths"),
    )
    if error:
        raise ValueError(error)
    work_dir, readable, writable = paths
    requirements = [
        ResourceRequirement("filesystem", str(work_dir), "write", "bash working directory")
    ]
    requirements.extend(
        ResourceRequirement("filesystem", str(path), "read", "bash declared read path")
        for path in readable
        if path != work_dir
    )
    requirements.extend(
        ResourceRequirement("filesystem", str(path), "write", "bash declared write path")
        for path in writable
        if path != work_dir
    )
    command = str(input_.get("command") or "")
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return requirements
    base = parts[0].lower().replace("\\", "/").split("/")[-1]
    spec = WHITELIST.get(base)
    if spec is None or not spec[1]:
        return requirements
    for value in parts[1:]:
        parsed = urlparse(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            target = f"{parsed.scheme}://{parsed.hostname}:{port}"
            break
    else:
        target = f"command:{base}"
    requirements.append(ResourceRequirement("network", target, "connect", f"bash {base}"))
    return requirements


def _resolve_execution_paths(
    *,
    cwd: object = "",
    read_paths: object = None,
    write_paths: object = None,
) -> tuple[tuple[Path, tuple[Path, ...], tuple[Path, ...]], str | None]:
    work_dir = _resolve_declared_path(str(cwd or _work_dir), base=_work_dir)
    if not work_dir.exists():
        return (work_dir, (), ()), f"Error: cwd does not exist: {cwd or _work_dir}"
    if not work_dir.is_dir():
        return (work_dir, (), ()), f"Error: cwd is not a directory: {cwd or _work_dir}"
    blocked = _blocked_path_error(work_dir)
    if blocked:
        return (work_dir, (), ()), blocked

    readable, error = _resolve_path_list(read_paths, base=work_dir, label="read_paths")
    if error:
        return (work_dir, (), ()), error
    writable, error = _resolve_path_list(write_paths, base=work_dir, label="write_paths")
    if error:
        return (work_dir, (), ()), error
    return (work_dir, readable, writable), None


def _resolve_path_list(
    values: object,
    *,
    base: Path,
    label: str,
) -> tuple[tuple[Path, ...], str | None]:
    if values is None:
        return (), None
    if not isinstance(values, (list, tuple)) or any(
        not isinstance(item, str) for item in values
    ):
        return (), f"Error: {label} must be a list of paths"
    resolved: list[Path] = []
    for value in values:
        path = _resolve_declared_path(value, base=base)
        blocked = _blocked_path_error(path)
        if blocked:
            return (), blocked
        if not path.exists():
            return (), f"Error: declared path does not exist: {value}"
        if path not in resolved:
            resolved.append(path)
    return tuple(resolved), None


def _resolve_declared_path(value: str, *, base: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _blocked_path_error(path: Path) -> str | None:
    from luna_agent.tools.sandbox import get_sandbox

    return get_sandbox().check_blocked_path(path)


def _collect_blocked_mounts(paths: Iterable[Path]) -> tuple[Path, ...]:
    from luna_agent.tools.sandbox import get_sandbox

    sandbox = get_sandbox()
    masked: set[Path] = set()
    inspected = 0
    deadline = time.monotonic() + 2.0
    for root in {Path(item).resolve() for item in paths}:
        if not root.is_dir():
            continue
        for current, dirs, files in os.walk(root, followlinks=False):
            inspected += len(dirs) + len(files)
            if inspected > 50_000 or time.monotonic() > deadline:
                raise RuntimeError(
                    "strict sandbox mount scan exceeded its safety budget; use a narrower cwd"
                )
            current_path = Path(current)
            visible_dirs: list[str] = []
            for name in dirs:
                candidate = current_path / name
                if _mount_path_is_blocked(sandbox, candidate, directory=True):
                    masked.add(candidate.absolute())
                else:
                    visible_dirs.append(name)
            dirs[:] = visible_dirs
            for name in files:
                candidate = current_path / name
                if _mount_path_is_blocked(sandbox, candidate):
                    masked.add(candidate.absolute())
    return tuple(sorted(masked, key=str))


def _mount_path_is_blocked(sandbox, path: Path, *, directory: bool = False) -> bool:
    if sandbox.check_blocked_path(path):
        return True
    return bool(
        directory
        and sandbox.check_blocked_path(path / "__luna_blocked_path_probe__")
    )


tool_registry.register(ToolEntry(
    name="bash",
    description="Execute a short bounded shell command in the configured sandbox. "
                "Only whitelisted commands are allowed. Use process_start for servers, watchers, "
                "long-running tests, or builds. Network commands are blocked unless execution policy allows them.",
    schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command, e.g. 'ls -la' or 'python --version'"},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 30, max 60)"},
            "cwd": {"type": "string", "description": "Working directory. It is a writable resource for this call."},
            "read_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Additional existing files or directories mounted read-only after approval.",
            },
            "write_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Additional existing files or directories mounted writable after approval.",
            },
        },
        "required": ["command"],
    },
    handler=_bash,
    toolset="builtin",
    permission_category="bash",
    tags=["terminal", "command", "shell"],
    risk_level="high",
    approval_mode="cached",
    usage_hint="Use for short bounded inspection or maintenance commands; use process_start for long-running work.",
    precheck=_precheck,
    resource_resolver=resource_requirements,
    is_parallel_safe=False,
    is_destructive=False,  # whitelist constrains safety
))
