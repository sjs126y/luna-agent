"""OS process sandbox selection and command construction."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Iterable

PROCESS_SANDBOX_BACKENDS = {"auto", "bwrap", "legacy"}


@dataclass(frozen=True)
class ProcessLaunchSpec:
    backend: str
    argv: tuple[str, ...]
    cwd: Path
    filesystem_isolated: bool
    network_isolated: bool
    warning: str = ""


def build_process_launch(
    command: str,
    *,
    cwd: Path,
    writable_roots: Iterable[Path],
    allow_network: bool,
    requested_backend: str = "auto",
) -> ProcessLaunchSpec:
    work_dir = cwd.resolve()
    capabilities = process_sandbox_capabilities()
    requested = normalize_process_backend(requested_backend)
    use_bwrap = requested == "bwrap" or (
        requested == "auto" and capabilities["bwrap_available"]
    )
    if requested == "bwrap" and not capabilities["bwrap_available"]:
        return ProcessLaunchSpec(
            backend="unavailable",
            argv=(),
            cwd=work_dir,
            filesystem_isolated=False,
            network_isolated=False,
            warning="bwrap was requested but is unavailable",
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

    binary = str(capabilities["bwrap_path"])
    argv = [binary, "--die-with-parent", "--ro-bind", "/", "/"]
    if Path("/tmp").exists():
        argv.extend(["--tmpfs", "/tmp"])
    roots = {Path(root).resolve() for root in writable_roots}
    roots.add(work_dir)
    for root in sorted(roots, key=str):
        if root.exists():
            argv.extend(["--bind", str(root), str(root)])
    network_isolated = bool(
        not allow_network and capabilities["network_namespace_available"]
    )
    if network_isolated:
        argv.append("--unshare-net")
    argv.extend(["--chdir", str(work_dir), "--", "/bin/sh", "-c", command])
    warning = ""
    if not allow_network and not network_isolated:
        warning = "network namespace unavailable; command whitelist remains the network boundary"
    return ProcessLaunchSpec(
        backend="bwrap",
        argv=tuple(argv),
        cwd=work_dir,
        filesystem_isolated=True,
        network_isolated=network_isolated,
        warning=warning,
    )


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
    warnings: list[str] = []
    if requested == "bwrap" and effective == "unavailable":
        warnings.append("bwrap requested but unavailable")
    if effective != "bwrap":
        warnings.append("process filesystem isolation is unavailable")
    elif not capabilities["network_namespace_available"]:
        warnings.append("bwrap network namespace is unavailable")
    return {
        "requested_backend": requested,
        "effective_backend": effective,
        "filesystem_isolated": effective == "bwrap",
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
