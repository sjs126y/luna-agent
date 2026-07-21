"""OS process sandbox selection and command construction."""

from __future__ import annotations

import asyncio
import os
import base64
from dataclasses import dataclass
from functools import lru_cache
import logging
from pathlib import Path
import secrets
import shutil
import shlex
import subprocess
import sys
from typing import Any, Iterable, Literal

logger = logging.getLogger(__name__)

PROCESS_SANDBOX_BACKENDS = {"auto", "bwrap", "appcontainer", "legacy"}


def _is_windows() -> bool:
    return os.name == "nt"


@dataclass(frozen=True)
class ProcessLaunchSpec:
    backend: str
    argv: tuple[str, ...]
    cwd: Path
    filesystem_isolated: bool
    network_isolated: bool
    warning: str = ""
    process_tree_managed: bool = False
    security_level: Literal["os-isolated", "controlled-host", "none"] = "none"
    # Native Windows shell launches use a one-shot broker.  The request is
    # sent over broker stdin, never encoded into the command line.
    broker_request: dict[str, Any] | None = None


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


async def spawn_process(
    launch: ProcessLaunchSpec,
    *,
    command: str,
    environment: dict[str, str],
    stdout,
    stderr,
    blocked_patterns: Iterable[str] = (),
) -> asyncio.subprocess.Process:
    """Start a process from a platform launch spec.

    Keeping broker protocol, stdio setup, and host Job Object handling here
    means ``bash`` and ``process_start`` share one backend implementation.
    """
    if launch.backend == "unavailable":
        raise RuntimeError(launch.warning)
    kwargs: dict[str, Any] = {
        "stdout": stdout,
        "stderr": stderr,
        "env": environment,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    if launch.backend == "windows-appcontainer":
        import json

        request = dict(launch.broker_request or {})
        request["environment"] = dict(environment)
        request["blocked_patterns"] = list(blocked_patterns)
        kwargs["stdin"] = asyncio.subprocess.PIPE
        proc = await asyncio.create_subprocess_exec(
            *launch.argv,
            cwd=str(launch.cwd),
            **kwargs,
        )
        # These private attributes are the cleanup contract consumed by the
        # shell tool and process tracker; the underlying handle remains a
        # normal asyncio subprocess process.
        proc._luna_appcontainer_broker = True
        proc._luna_broker_request = request
        try:
            payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            proc.stdin.write(payload)
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception:
            proc.kill()
            await proc.wait()
            cleanup_shell_broker_request(request)
            raise
    elif launch.backend in {"bwrap", "windows-powershell"}:
        proc = await asyncio.create_subprocess_exec(
            *launch.argv,
            cwd=str(launch.cwd),
            **kwargs,
        )
    else:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(launch.cwd),
            **kwargs,
        )

    if os.name == "nt" and launch.backend != "windows-appcontainer":
        from luna_agent.tools import windows_job

        if not windows_job.attach(proc.pid):
            try:
                proc.kill()
            finally:
                await proc.wait()
            raise RuntimeError("Windows Job Object is unavailable; refusing to start process")
    return proc


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
    if requested == "appcontainer":
        return ProcessLaunchSpec(
            backend="unavailable",
            argv=(),
            cwd=work_dir,
            filesystem_isolated=False,
            network_isolated=False,
            warning="AppContainer is a native Windows backend; it is unavailable on this platform",
            security_level="none",
        )
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
            security_level="none",
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
            security_level="none",
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
        process_tree_managed=False,
        security_level="os-isolated",
    )


def build_shell_process_launch(
    command: str,
    *,
    cwd: Path,
    writable_roots: Iterable[Path],
    readable_roots: Iterable[Path] = (),
    masked_paths: Iterable[Path] = (),
    allow_network: bool,
    requested_backend: str = "auto",
    policy: ProcessSandboxPolicy = BASH_STRICT_POLICY,
) -> ProcessLaunchSpec:
    """Build the platform-specific launch used by the built-in shell tools.

    MCP stdio keeps using :func:`build_process_launch` because its configured
    command and argv must remain native executable arguments.  Only the
    user-facing shell tools use PowerShell on Windows.
    """
    if _is_windows():
        return _build_windows_shell_launch(
            command,
            cwd=cwd,
            writable_roots=writable_roots,
            readable_roots=readable_roots,
            masked_paths=masked_paths,
            allow_network=allow_network,
            requested_backend=requested_backend,
            policy=policy,
        )
    return build_process_launch(
        command,
        cwd=cwd,
        writable_roots=writable_roots,
        readable_roots=readable_roots,
        masked_paths=masked_paths,
        allow_network=allow_network,
        requested_backend=requested_backend,
        policy=policy,
    )


def _build_windows_shell_launch(
    command: str,
    *,
    cwd: Path,
    writable_roots: Iterable[Path],
    readable_roots: Iterable[Path],
    masked_paths: Iterable[Path],
    allow_network: bool,
    requested_backend: str,
    policy: ProcessSandboxPolicy,
) -> ProcessLaunchSpec:
    """Select native Windows AppContainer, or explicit legacy mode."""
    work_dir = Path(cwd).resolve()
    requested = normalize_process_backend(requested_backend)
    capabilities = process_sandbox_capabilities()
    powershell = str(capabilities.get("powershell_path") or "")
    if not powershell:
        return ProcessLaunchSpec(
            backend="unavailable",
            argv=(),
            cwd=work_dir,
            filesystem_isolated=False,
            network_isolated=False,
            warning="PowerShell 7 (pwsh.exe) is required on native Windows",
            security_level="none",
        )
    if requested == "bwrap":
        return ProcessLaunchSpec(
            backend="unavailable",
            argv=(),
            cwd=work_dir,
            filesystem_isolated=False,
            network_isolated=False,
            warning="Bubblewrap is not a native Windows shell backend; use appcontainer or legacy",
            security_level="none",
        )
    if requested == "legacy":
        return _build_windows_powershell_launch(
            command,
            cwd=work_dir,
            requested_backend=requested,
            policy=policy,
        )
    if not bool(capabilities.get("appcontainer_available", True)):
        return ProcessLaunchSpec(
            backend="unavailable",
            argv=(),
            cwd=work_dir,
            filesystem_isolated=False,
            network_isolated=False,
            warning="Native Windows AppContainer support is unavailable",
            security_level="none",
        )

    roots = tuple(Path(root).resolve() for root in writable_roots)
    readable = tuple(Path(root).resolve() for root in readable_roots)
    profile = _shell_profile_name(work_dir, command)
    request: dict[str, Any] = {
        "schema_version": 1,
        "command": str(command),
        "cwd": str(work_dir),
        "sandbox_roots": [str(work_dir), *(str(root) for root in roots)],
        "read_roots": [str(root) for root in readable],
        "write_roots": [str(root) for root in roots if root != work_dir],
        "masked_paths": [str(Path(path).resolve()) for path in masked_paths],
        "allow_network": bool(allow_network),
        "environment": {},
        "profile_name": profile,
        "lease_root": str(work_dir / ".luna-agent-leases"),
        "acl_roots": [str(work_dir), *(str(root) for root in readable), *(str(root) for root in roots)],
    }
    broker = str(Path(sys.executable).resolve())
    return ProcessLaunchSpec(
        backend="windows-appcontainer",
        argv=(broker, "-m", "luna_agent.tools.windows_shell_broker"),
        cwd=work_dir,
        filesystem_isolated=True,
        network_isolated=not allow_network,
        warning="",
        process_tree_managed=True,
        security_level="os-isolated",
        broker_request=request,
    )


def _shell_profile_name(cwd: Path, command: str) -> str:
    from luna_agent.security.windows_appcontainer import profile_name

    identity = f"{cwd.resolve()}\0{secrets.token_hex(12)}\0{command[:128]}"
    return profile_name("Shell", identity)


def cleanup_shell_broker_request(request: dict[str, Any] | None) -> None:
    """Best-effort cleanup for a broker killed before its finally block."""
    if not request:
        return
    profile = request.get("profile_name")
    roots = request.get("acl_roots") or ()
    if not isinstance(profile, str):
        return
    try:
        from luna_agent.security.windows_appcontainer import (
            cleanup_appcontainer_profile,
            sweep_orphan_leases,
        )

        lease_root = request.get("lease_root")
        if isinstance(lease_root, str) and lease_root:
            sweep_orphan_leases(Path(lease_root))
        else:
            cleanup_appcontainer_profile(profile, tuple(Path(root) for root in roots))
    except Exception:
        # Cleanup is retried by the broker/profile orphan sweep; never mask
        # the original shell process result with an ACL cleanup error.
        logger.debug("Shell broker cleanup failed for %s", profile, exc_info=True)


def _build_windows_powershell_launch(
    command: str,
    *,
    cwd: Path,
    requested_backend: str,
    policy: ProcessSandboxPolicy,
) -> ProcessLaunchSpec:
    work_dir = Path(cwd).resolve()
    requested = normalize_process_backend(requested_backend)
    powershell = str(process_sandbox_capabilities().get("powershell_path") or "")
    if not powershell:
        return ProcessLaunchSpec(
            backend="unavailable",
            argv=(),
            cwd=work_dir,
            filesystem_isolated=False,
            network_isolated=False,
            warning="PowerShell 7 (pwsh.exe) is required on native Windows",
            security_level="none",
        )
    if requested == "bwrap":
        return ProcessLaunchSpec(
            backend="unavailable",
            argv=(),
            cwd=work_dir,
            filesystem_isolated=False,
            network_isolated=False,
            warning="Bubblewrap is not a native Windows shell backend; use PowerShell 7",
            security_level="none",
        )
    script = (
        "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$global:PSNativeCommandEncoding = [System.Text.UTF8Encoding]::new($false); "
        f"{command}"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    warning = ""
    if policy.strict_filesystem or requested == "legacy":
        warning = "Windows built-in shell uses controlled-host policy; filesystem isolation is unavailable"
    return ProcessLaunchSpec(
        backend="windows-powershell",
        argv=(
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded,
        ),
        cwd=work_dir,
        filesystem_isolated=False,
        network_isolated=False,
        warning=warning,
        process_tree_managed=True,
        security_level="controlled-host",
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
    argv = [
        binary,
        "--die-with-parent",
        "--ro-bind",
        "/",
        "/",
        "--unshare-user-try",
    ]
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
        "--unshare-user-try",
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
    powershell_path = shutil.which("pwsh") if _is_windows() else None
    return {
        "platform": sys.platform,
        "bwrap_path": path or "",
        "bwrap_available": available,
        "powershell_path": powershell_path or "",
        "powershell_available": bool(powershell_path),
        "appcontainer_available": _is_windows(),
        "shell_broker_available": _is_windows(),
        "lease_recovery_available": _is_windows(),
        "job_object_available": _is_windows(),
        "network_namespace_available": (
            _probe_network_namespace(path) if available else False
        ),
    }


def process_sandbox_snapshot(requested_backend: object = "auto") -> dict[str, object]:
    requested = normalize_process_backend(requested_backend)
    capabilities = process_sandbox_capabilities()
    windows = _is_windows()
    windows_shell = windows and bool(capabilities.get("powershell_available"))
    windows_appcontainer = windows_shell and bool(
        capabilities.get("appcontainer_available", False)
    )
    if windows:
        # Native Windows has no Bubblewrap path.  Keep an explicit bwrap
        # request fail-closed instead of silently selecting another backend.
        if requested in {"bwrap", "appcontainer"} and not windows_appcontainer:
            effective = "unavailable"
            bash_effective = "unavailable"
        elif requested == "bwrap" or not windows_shell:
            effective = "unavailable"
            bash_effective = "unavailable"
        elif requested == "legacy":
            effective = "windows-powershell"
            bash_effective = "windows-powershell"
        elif windows_appcontainer:
            effective = "windows-appcontainer"
            bash_effective = "windows-appcontainer"
        else:
            effective = "unavailable"
            bash_effective = "unavailable"
    else:
        if requested == "appcontainer":
            effective = "unavailable"
            bash_effective = "unavailable"
        elif capabilities["bwrap_available"] and requested in {"auto", "bwrap"}:
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
    if requested == "appcontainer" and effective == "unavailable":
        warnings.append("AppContainer is available only on native Windows")
    if windows and not capabilities.get("powershell_available"):
        warnings.append("PowerShell 7 (pwsh.exe) is required for native Windows shell tools")
    if windows and requested == "appcontainer" and not windows_appcontainer:
        warnings.append("Windows AppContainer shell backend is unavailable")
    if effective == "windows-powershell":
        warnings.append("Windows built-in shell uses controlled-host policy")
    elif effective == "windows-appcontainer":
        pass
    elif effective != "bwrap":
        warnings.append("process filesystem isolation is unavailable")
    elif not capabilities["network_namespace_available"]:
        warnings.append("bwrap network namespace is unavailable")
    if bash_effective == "windows-powershell":
        pass
    elif bash_effective == "unavailable":
        warnings.append("strict Bash execution is unavailable without bwrap")
    elif bash_effective == "legacy":
        warnings.append("strict Bash filesystem isolation is explicitly disabled")
    return {
        "requested_backend": requested,
        "effective_backend": effective,
        "filesystem_isolated": effective in {"bwrap", "windows-appcontainer"},
        "bash_effective_backend": bash_effective,
        "bash_filesystem_isolated": bash_effective in {"bwrap", "windows-appcontainer"},
        "bash_fail_closed": (
            bash_effective == "unavailable" if windows else requested != "legacy"
        ),
        "process_tree_managed": bool(
            capabilities.get("job_object_available")
            and effective in {"windows-powershell", "windows-appcontainer"}
        ),
        "security_level": (
            "controlled-host" if effective == "windows-powershell"
            else "os-isolated" if effective in {"bwrap", "windows-appcontainer"}
            else "none"
        ),
        "powershell_path": str(capabilities.get("powershell_path") or ""),
        "powershell_available": bool(capabilities.get("powershell_available")),
        "appcontainer_available": bool(capabilities.get("appcontainer_available")),
        "shell_broker_available": bool(capabilities.get("shell_broker_available")),
        "lease_recovery_available": bool(
            capabilities.get("lease_recovery_available")
        ),
        "job_object_available": bool(capabilities.get("job_object_available")),
        "network_namespace_available": bool(
            capabilities["network_namespace_available"]
        ),
        "bwrap_path": str(capabilities["bwrap_path"]),
        "warnings": warnings,
    }


def _probe_network_namespace(binary: str | None) -> bool:
    if not binary or _is_windows():
        return False
    try:
        completed = subprocess.run(
            [
                binary,
                "--ro-bind",
                "/",
                "/",
                "--unshare-user-try",
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
            [
                binary,
                "--ro-bind",
                "/",
                "/",
                "--unshare-user-try",
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
