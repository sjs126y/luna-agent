"""Cross-platform protection for files containing credentials or tokens."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _is_windows() -> bool:
    return os.name == "nt"


def secure_file(path: Path, *, mode: int = 0o600) -> None:
    """Restrict a sensitive file to the current user on each platform."""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(target)
    if not _is_windows():
        target.chmod(mode)
        return

    identity = _windows_identity()
    completed = subprocess.run(
        ["icacls", str(target), "/inheritance:r", "/grant:r", f"{identity}:F"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stdout or "").strip()[-2000:]
        raise RuntimeError(f"Failed to protect sensitive file {target}: {detail}")


def _windows_identity() -> str:
    completed = subprocess.run(
        ["whoami"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
        check=False,
    )
    identity = (completed.stdout or "").strip().splitlines()[0] if completed.stdout else ""
    if completed.returncode != 0 or not identity:
        domain = os.environ.get("USERDOMAIN", "").strip()
        username = os.environ.get("USERNAME", "").strip()
        identity = f"{domain}\\{username}" if domain and username else username
    if not identity:
        raise RuntimeError("Unable to determine the current Windows user for ACL protection")
    return identity
