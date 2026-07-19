"""OS process sandbox selection and command construction."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
from typing import Iterable, Literal

PROCESS_SANDBOX_BACKENDS = {"auto", "bwrap", "legacy"}


@dataclass(frozen=True)
class ProcessLaunchSpec:
    backend: str
    argv: tuple[str, ...]
    cwd: Path
    filesystem_isolated: bool
    network_isolated: bool
    warning: str = ""


@dataclass(frozen=True)
class ProcessSandboxPolicy:
    name: str
    strict_filesystem: bool = False
    allow_legacy_fallback: bool = True
    isolated_home: bool = False


@dataclass(frozen=True)
class ProcessMount:
    source: Path
    target: Path
    access: Literal["read", "write"] = "read"
    reason: str = ""


@dataclass(frozen=True)
class ProcessMountPlan:
    policy: ProcessSandboxPolicy
    cwd: Path
    runtime_mounts: tuple[ProcessMount, ...] = ()
    user_mounts: tuple[ProcessMount, ...] = ()
    masked_paths: tuple[Path, ...] = ()
    network_isolated: bool = False


MCP_COMPATIBLE_POLICY = ProcessSandboxPolicy("mcp-compatible")
BASH_STRICT_POLICY = ProcessSandboxPolicy(
    "bash-strict",
    strict_filesystem=True,
    allow_legacy_fallback=False,
    isolated_home=True,
)


def build_process_launch(
    command: str,
    *,
    cwd: Path,
    writable_roots: Iterable[Path],
    readable_roots: Iterable[Path] = (),
    masked_paths: Iterable[Path] = (),
    allow_network: bool,
    requested_backend: str = "auto",
    policy: ProcessSandboxPolicy = MCP_COMPATIBLE_POLICY,
) -> ProcessLaunchSpec:
    work_dir = cwd.resolve()
    capabilities = process_sandbox_capabilities()
    requested = normalize_process_backend(requested_backend)
    use_bwrap = requested == "bwrap" or (
        requested == "auto" and capabilities["bwrap_available"]
    )
    if (
        requested == "bwrap"
        or requested == "auto" and not policy.allow_legacy_fallback
    ) and not capabilities["bwrap_available"]:
        return ProcessLaunchSpec(
            backend="unavailable",
            argv=(),
            cwd=work_dir,
            filesystem_isolated=False,
            network_isolated=False,
            warning=f"{policy.name} requires bwrap, but bwrap is unavailable",
        )
    if not use_bwrap:
        warning = ""
        return ProcessLaunchSpec(
            backend="legacy",
            argv=(command,),
            cwd=work_dir,
            filesystem_isolated=False,
            network_isolated=False,
            warning=warning,
        )

    plan = build_process_mount_plan(
        policy=policy,
        cwd=work_dir,
        command=command,
        readable_roots=readable_roots,
        writable_roots=writable_roots,
        masked_paths=masked_paths,
        network_isolated=bool(
            not allow_network and capabilities["network_namespace_available"]
        ),
    )
    binary = str(capabilities["bwrap_path"])
    argv = (
        _strict_bwrap_argv(binary, plan)
        if policy.strict_filesystem
        else _compatible_bwrap_argv(binary, plan)
    )
    argv.extend(["--", "/bin/sh", "-c", command])
    warning = ""
    if not allow_network and not plan.network_isolated:
        warning = "network namespace unavailable; command whitelist remains the network boundary"
    return ProcessLaunchSpec(
        backend="bwrap",
        argv=tuple(argv),
        cwd=work_dir,
        filesystem_isolated=True,
        network_isolated=plan.network_isolated,
        warning=warning,
    )


def build_process_mount_plan(
    *,
    policy: ProcessSandboxPolicy,
    cwd: Path,
    command: str = "",
    readable_roots: Iterable[Path] = (),
    writable_roots: Iterable[Path],
    masked_paths: Iterable[Path] = (),
    network_isolated: bool,
) -> ProcessMountPlan:
    work_dir = Path(cwd).resolve()
    write_roots = {Path(root).resolve() for root in writable_roots}
    write_roots.add(work_dir)
    read_roots = {
        Path(root).resolve()
        for root in readable_roots
        if Path(root).resolve() not in write_roots
    }
    runtime_mounts: list[ProcessMount] = []
    executable = _command_executable(command)
    if executable is not None and not _is_system_runtime_path(executable):
        runtime_mounts.append(
            ProcessMount(executable, executable, "read", "command executable")
        )
    return ProcessMountPlan(
        policy=policy,
        cwd=work_dir,
        runtime_mounts=tuple(runtime_mounts),
        user_mounts=tuple(
            [
                ProcessMount(root, root, "read", "declared readable path")
                for root in sorted(read_roots, key=str)
                if root.exists()
            ]
            + [
                ProcessMount(root, root, "write", "declared writable path")
                for root in sorted(write_roots, key=str)
                if root.exists()
            ]
        ),
        masked_paths=tuple(
            path
            for path in sorted({Path(item).absolute() for item in masked_paths}, key=str)
            if path.exists()
        ),
        network_isolated=network_isolated,
    )


def _compatible_bwrap_argv(binary: str, plan: ProcessMountPlan) -> list[str]:
    argv = [binary, "--die-with-parent", "--ro-bind", "/", "/"]
    if Path("/tmp").exists():
        argv.extend(["--tmpfs", "/tmp"])
    if Path("/dev").exists():
        argv.extend(["--dev", "/dev"])
    for mount in plan.user_mounts:
        operation = "--bind" if mount.access == "write" else "--ro-bind"
        argv.extend([operation, str(mount.source), str(mount.target)])
    if plan.network_isolated:
        argv.append("--unshare-net")
    argv.extend(["--chdir", str(plan.cwd)])
    return argv


def _strict_bwrap_argv(binary: str, plan: ProcessMountPlan) -> list[str]:
    argv = [
        binary,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/sbin",
        "/sbin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--tmpfs",
        "/tmp",
    ]
    for source in _runtime_system_paths():
        _append_mount(argv, ProcessMount(source, source, "read", "system runtime"))
    for mount in (*plan.runtime_mounts, *plan.user_mounts):
        _append_mount(argv, mount)
    for path in plan.masked_paths:
        _append_parent_dirs(argv, path)
        if path.is_dir():
            argv.extend(["--tmpfs", str(path)])
        else:
            argv.extend(["--ro-bind", "/dev/null", str(path)])
    if plan.network_isolated:
        argv.append("--unshare-net")
    if plan.policy.isolated_home:
        home = Path("/home/luna")
        _append_parent_dirs(argv, home)
        argv.extend(["--dir", str(home), "--setenv", "HOME", str(home)])
        argv.extend(["--setenv", "XDG_CACHE_HOME", "/tmp/luna-cache"])
    argv.extend(["--setenv", "TMPDIR", "/tmp", "--chdir", str(plan.cwd)])
    return argv


def _append_mount(argv: list[str], mount: ProcessMount) -> None:
    if not mount.source.exists():
        return
    _append_parent_dirs(argv, mount.target)
    operation = "--bind" if mount.access == "write" else "--ro-bind"
    argv.extend([operation, str(mount.source), str(mount.target)])


def _append_parent_dirs(argv: list[str], target: Path) -> None:
    parents = list(target.parents)
    for parent in reversed(parents[:-1]):
        value = str(parent)
        if value != "/" and not _argv_creates_target(argv, value):
            argv.extend(["--dir", value])


def _argv_creates_target(argv: list[str], target: str) -> bool:
    return target in argv


def _runtime_system_paths() -> tuple[Path, ...]:
    candidates = (
        "/etc/alternatives",
        "/etc/ca-certificates",
        "/etc/ssl",
        "/etc/hosts",
        "/etc/resolv.conf",
        "/etc/nsswitch.conf",
        "/etc/passwd",
        "/etc/group",
        "/etc/ld.so.cache",
        "/etc/localtime",
    )
    return tuple(Path(item) for item in candidates if Path(item).exists())


def _command_executable(command: str) -> Path | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return None
    path = shutil.which(parts[0])
    return Path(path).absolute() if path else None


def _is_system_runtime_path(path: Path) -> bool:
    resolved = path.resolve()
    return resolved == Path("/usr") or Path("/usr") in resolved.parents


def normalize_process_backend(value: object) -> str:
    backend = str(value or "auto").strip().lower()
    return backend if backend in PROCESS_SANDBOX_BACKENDS else "auto"


@lru_cache(maxsize=1)
def process_sandbox_capabilities() -> dict[str, object]:
    path = shutil.which("bwrap") if sys.platform.startswith("linux") else None
    available = bool(path and _probe_bwrap(path))
    return {
        "platform": sys.platform,
        "bwrap_path": path or "",
        "bwrap_available": available,
        "network_namespace_available": (
            _probe_network_namespace(path) if available else False
        ),
    }


def process_sandbox_snapshot(requested_backend: object = "auto") -> dict[str, object]:
    requested = normalize_process_backend(requested_backend)
    capabilities = process_sandbox_capabilities()
    if capabilities["bwrap_available"] and requested in {"auto", "bwrap"}:
        effective = "bwrap"
    elif requested == "bwrap":
        effective = "unavailable"
    else:
        effective = "legacy"
    if requested == "legacy":
        bash_effective = "legacy"
    elif capabilities["bwrap_available"]:
        bash_effective = "bwrap"
    else:
        bash_effective = "unavailable"
    warnings: list[str] = []
    if requested == "bwrap" and effective == "unavailable":
        warnings.append("bwrap requested but unavailable")
    if effective != "bwrap":
        warnings.append("process filesystem isolation is unavailable")
    elif not capabilities["network_namespace_available"]:
        warnings.append("bwrap network namespace is unavailable")
    if bash_effective == "unavailable":
        warnings.append("strict Bash execution is unavailable without bwrap")
    elif bash_effective == "legacy":
        warnings.append("strict Bash filesystem isolation is explicitly disabled")
    return {
        "requested_backend": requested,
        "effective_backend": effective,
        "filesystem_isolated": effective == "bwrap",
        "bash_effective_backend": bash_effective,
        "bash_filesystem_isolated": bash_effective == "bwrap",
        "bash_fail_closed": requested != "legacy",
        "network_namespace_available": bool(
            capabilities["network_namespace_available"]
        ),
        "bwrap_path": str(capabilities["bwrap_path"]),
        "warnings": warnings,
    }


def _probe_network_namespace(binary: str | None) -> bool:
    if not binary or os.name == "nt":
        return False
    try:
        completed = subprocess.run(
            [
                binary,
                "--ro-bind",
                "/",
                "/",
                "--unshare-net",
                "--",
                "/bin/true",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _probe_bwrap(binary: str) -> bool:
    try:
        completed = subprocess.run(
            [binary, "--ro-bind", "/", "/", "--", "/bin/true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0
