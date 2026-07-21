"""One-shot native Windows PowerShell AppContainer broker.

The host shell tool starts this module as a normal process, sends one bounded
JSON request over stdin, and then treats the broker's stdout/stderr as the
command output.  The broker validates the request again and starts PowerShell
inside an AppContainer Job Object.  No command or environment is placed in
the broker command line.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
from types import SimpleNamespace
from typing import Any, BinaryIO, Iterable

from luna_agent.security.windows_appcontainer import AppContainerLease
from luna_agent.tools.env_filter import filter_env
from luna_agent.tools.shell_policy import check_command

_SCHEMA_VERSION = 1
_MAX_REQUEST_BYTES = 1_000_000
_PROFILE_RE = re.compile(r"^LunaAgent\.[A-Za-z0-9_.-]{8,96}$")
_EXECUTABLE_NAMES = (
    "python", "python3", "git", "uv", "npm", "npx", "node", "pip",
    "cargo", "go", "rustc", "gcc", "g++", "make", "where", "cmd",
)
_DEFAULT_BLOCKED_PATTERNS = (
    "**/.env", "**/.env.*", "**/.git/**", "**/id_rsa*", "**/*credentials*",
)


@dataclass(frozen=True, slots=True)
class BrokerRequest:
    command: str
    cwd: Path
    sandbox_roots: tuple[Path, ...]
    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]
    masked_paths: tuple[Path, ...]
    allow_network: bool
    environment: dict[str, str]
    profile_name: str
    lease_root: Path
    acl_roots: tuple[Path, ...]
    blocked_patterns: tuple[str, ...]


def main() -> int:
    try:
        request = _read_request()
        validated = _validate_request(request)
        return _run(validated)
    except Exception as exc:
        print(f"Windows Shell Broker error: {exc}", file=sys.stderr, flush=True)
        return 2


def _read_request() -> dict[str, Any]:
    data = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
    if len(data) > _MAX_REQUEST_BYTES:
        raise ValueError("broker request is too large")
    if not data:
        raise ValueError("broker request is empty")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("broker request is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("broker request must be a JSON object")
    return value


def _validate_request(raw: dict[str, Any]) -> BrokerRequest:
    if raw.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("unsupported broker request schema")
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise ValueError("command must be a non-empty string without NUL bytes")
    if len(command) > 32_000:
        raise ValueError("command is too long")

    cwd = _required_path(raw, "cwd", must_exist=True, directory=True)
    sandbox_roots = _path_list(raw, "sandbox_roots", must_exist=True)
    if cwd not in sandbox_roots:
        sandbox_roots = (cwd, *sandbox_roots)
    read_roots = _path_list(raw, "read_roots", must_exist=True)
    write_roots = _path_list(raw, "write_roots", must_exist=True)
    if cwd not in write_roots:
        write_roots = (cwd, *write_roots)
    masked_paths = _path_list(raw, "masked_paths", must_exist=False)

    profile = raw.get("profile_name")
    if not isinstance(profile, str) or not _PROFILE_RE.fullmatch(profile):
        raise ValueError("invalid AppContainer profile name")
    acl_roots = _path_list(raw, "acl_roots", must_exist=False)
    if not acl_roots:
        acl_roots = tuple(dict.fromkeys((*sandbox_roots, *read_roots, *write_roots)))
    lease_value = raw.get("lease_root") or str(cwd / ".luna-agent-leases")
    if not isinstance(lease_value, str) or "\x00" in lease_value:
        raise ValueError("lease_root must be an absolute path")
    lease_root = Path(lease_value).resolve()
    if not lease_root.is_absolute() or cwd not in (lease_root, *lease_root.parents):
        raise ValueError("lease_root must be inside the working directory")

    environment_raw = raw.get("environment")
    if not isinstance(environment_raw, dict):
        raise ValueError("environment must be an object")
    environment = {
        str(key): str(value)
        for key, value in environment_raw.items()
        if isinstance(key, str) and isinstance(value, (str, int, float, bool))
    }
    if any("\x00" in key or "\x00" in value for key, value in environment.items()):
        raise ValueError("environment contains NUL bytes")
    environment = filter_env(environment)

    blocked = raw.get("blocked_patterns", _DEFAULT_BLOCKED_PATTERNS)
    if not isinstance(blocked, (list, tuple)) or any(not isinstance(item, str) for item in blocked):
        raise ValueError("blocked_patterns must be a list of strings")

    # Re-run the same command policy inside the trust boundary.  Declared
    # roots are passed as resources, so path approval remains explicit.
    policy_sandbox = SimpleNamespace(blocked=tuple(blocked), roots=sandbox_roots)
    policy_error = check_command(
        command,
        declared_paths=(*sandbox_roots, *read_roots, *write_roots),
        allow_network=bool(raw.get("allow_network", False)),
        restrict_paths=True,
        is_windows=True,
        sandbox=policy_sandbox,
    )
    if policy_error:
        raise ValueError(policy_error)

    return BrokerRequest(
        command=command,
        cwd=cwd,
        sandbox_roots=tuple(dict.fromkeys(sandbox_roots)),
        read_roots=tuple(dict.fromkeys(read_roots)),
        write_roots=tuple(dict.fromkeys(write_roots)),
        masked_paths=tuple(dict.fromkeys(masked_paths)),
        allow_network=bool(raw.get("allow_network", False)),
        environment=environment,
        profile_name=profile,
        lease_root=lease_root,
        acl_roots=tuple(dict.fromkeys(acl_roots)),
        blocked_patterns=tuple(blocked),
    )


def _required_path(
    raw: dict[str, Any],
    key: str,
    *,
    must_exist: bool,
    directory: bool = False,
) -> Path:
    value = raw.get(key)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{key} must be an absolute path")
    path = Path(value).resolve()
    if not path.is_absolute():
        raise ValueError(f"{key} must be absolute")
    if must_exist and not path.exists():
        raise ValueError(f"{key} does not exist: {path}")
    if directory and not path.is_dir():
        raise ValueError(f"{key} is not a directory: {path}")
    return path


def _path_list(raw: dict[str, Any], key: str, *, must_exist: bool) -> tuple[Path, ...]:
    values = raw.get(key, [])
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{key} must be a list of paths")
    result: list[Path] = []
    for value in values:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError(f"{key} contains an invalid path")
        path = Path(value).resolve()
        if not path.is_absolute():
            raise ValueError(f"{key} must contain absolute paths")
        if must_exist and not path.exists():
            raise ValueError(f"{key} path does not exist: {path}")
        if path not in result:
            result.append(path)
    return tuple(result)


def _run(request: BrokerRequest) -> int:
    if os.name != "nt":
        raise RuntimeError("AppContainer Shell Broker is available only on native Windows")
    powershell = _find_powershell(request.environment)
    runtime_roots = _runtime_roots(powershell, request.environment)
    temp_root = request.cwd / ".luna-agent-tmp"
    created_temp = False
    if not temp_root.exists():
        temp_root.mkdir(parents=True, exist_ok=True)
        created_temp = True
    env = dict(request.environment)
    env.setdefault("SystemRoot", os.environ.get("SystemRoot", ""))
    env["TEMP"] = str(temp_root)
    env["TMP"] = str(temp_root)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["PATH"] = _safe_path(env.get("PATH", ""), runtime_roots)

    denied_roots = tuple(dict.fromkeys((*request.masked_paths, request.lease_root)))
    acl_roots = tuple(
        dict.fromkeys(
            (*request.acl_roots, *runtime_roots, temp_root, *denied_roots)
        )
    )
    lease = AppContainerLease(
        profile_name=request.profile_name,
        roots=acl_roots,
        active_process_limit=64,
        lease_root=request.lease_root,
    )
    process = None
    try:
        process = lease.spawn(
            command=_powershell_command(powershell, request.command),
            cwd=request.cwd,
            env=env,
            readable_roots=tuple(dict.fromkeys((*request.read_roots, *runtime_roots))),
            writable_roots=tuple(dict.fromkeys((*request.write_roots, temp_root))),
            denied_roots=denied_roots,
            allow_network=request.allow_network,
        )
        # Built-in commands do not receive an interactive stdin.  EOF is less
        # surprising than allowing PowerShell to block waiting for user input.
        process.stdin.close()
        relays = (
            _start_relay(process.stdout, sys.stdout.buffer),
            _start_relay(process.stderr, sys.stderr.buffer),
        )
        code = process.wait()
        for thread in relays:
            thread.join(timeout=5)
        return int(code)
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
            except Exception:
                pass
            process.close()
        lease.close()
        if created_temp:
            shutil.rmtree(temp_root, ignore_errors=True)


def _find_powershell(environment: dict[str, str]) -> Path:
    path_value = environment.get("PATH", "")
    found = shutil.which("pwsh.exe", path=path_value) or shutil.which("pwsh", path=path_value)
    candidates = [Path(found)] if found else []
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates.append(Path(program_files) / "PowerShell" / "7" / "pwsh.exe")
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    raise RuntimeError("PowerShell 7 (pwsh.exe) is not available")


def _runtime_roots(powershell: Path, environment: dict[str, str]) -> tuple[Path, ...]:
    roots: list[Path] = [powershell.parent]
    system_root = os.environ.get("SystemRoot") or environment.get("SystemRoot")
    if system_root:
        roots.extend((Path(system_root), Path(system_root) / "System32"))
    path_value = environment.get("PATH", "")
    for name in _EXECUTABLE_NAMES:
        found = shutil.which(name, path=path_value)
        if found:
            roots.append(Path(found).resolve().parent)
    return tuple(dict.fromkeys(root.resolve() for root in roots if root.exists()))


def _safe_path(value: str, runtime_roots: Iterable[Path]) -> str:
    entries = [str(root) for root in runtime_roots]
    for item in str(value or "").split(os.pathsep):
        try:
            path = Path(item).resolve()
        except (OSError, ValueError):
            continue
        if path.exists() and path not in runtime_roots:
            entries.append(str(path))
    return os.pathsep.join(dict.fromkeys(entries))


def _powershell_command(powershell: Path, command: str) -> tuple[str, ...]:
    script = (
        "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$global:PSNativeCommandEncoding = [System.Text.UTF8Encoding]::new($false); "
        f"{command}"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return (
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        encoded,
    )


def _start_relay(source: BinaryIO, target: BinaryIO) -> threading.Thread:
    def relay() -> None:
        try:
            while True:
                chunk = source.read(8192)
                if not chunk:
                    return
                target.write(chunk)
                target.flush()
        except (BrokenPipeError, OSError):
            return

    thread = threading.Thread(target=relay, name="luna-shell-broker-relay", daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":  # pragma: no cover - exercised by Windows CI
    raise SystemExit(main())
