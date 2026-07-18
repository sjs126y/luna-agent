"""Bounded filesystem traversal shared by file search tools."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import time
from typing import Callable

from personal_agent.tools.sandbox import Sandbox


DEFAULT_SCAN_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_SCANNED_ENTRIES = 50_000
SKIPPED_DIRECTORY_NAMES = frozenset({
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
})


@dataclass
class FileScanResult:
    files: list[Path]
    scanned_entries: int = 0
    skipped_directories: int = 0
    blocked_error: str = ""
    truncated_reason: str = ""


def scan_files(
    root: Path,
    sandbox: Sandbox,
    *,
    accept: Callable[[Path, str], bool] | None = None,
    blocked_accept: Callable[[Path, str], bool] | None = None,
    max_files: int | None = None,
    max_depth: int | None = None,
    include_hidden: bool = False,
    timeout_seconds: float = DEFAULT_SCAN_TIMEOUT_SECONDS,
    max_scanned_entries: int = DEFAULT_MAX_SCANNED_ENTRIES,
) -> FileScanResult:
    """Walk files without following symlinks, with pruning and hard budgets."""
    result = FileScanResult(files=[])
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    stack = [(root, 0)]

    while stack:
        if time.monotonic() >= deadline:
            result.truncated_reason = f"time budget ({timeout_seconds:g}s) reached"
            break
        current, current_depth = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda item: item.name.lower())
        except (OSError, PermissionError):
            continue

        child_directories: list[Path] = []
        for entry in entries:
            result.scanned_entries += 1
            if result.scanned_entries > max_scanned_entries:
                result.truncated_reason = (
                    f"scan budget ({max_scanned_entries} entries) reached"
                )
                return result
            if time.monotonic() >= deadline:
                result.truncated_reason = f"time budget ({timeout_seconds:g}s) reached"
                return result

            name = entry.name
            path = Path(entry.path)
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue

            if is_directory:
                if name in SKIPPED_DIRECTORY_NAMES or (
                    name.startswith(".") and not include_hidden
                ):
                    result.skipped_directories += 1
                    continue
                error = sandbox.check_path(path)
                if error:
                    relative = path.relative_to(root).as_posix()
                    if (
                        "path blocked by sandbox" in error.lower()
                        and not result.blocked_error
                        and blocked_accept is not None
                        and blocked_accept(path, relative)
                    ):
                        result.blocked_error = error
                    result.skipped_directories += 1
                    continue
                if max_depth is None or current_depth + 1 < max_depth:
                    child_directories.append(path)
                continue

            if not is_file or (name.startswith(".") and not include_hidden):
                continue
            relative = path.relative_to(root).as_posix()
            error = sandbox.check_path(path)
            if error:
                if (
                    "path blocked by sandbox" in error.lower()
                    and not result.blocked_error
                    and blocked_accept is not None
                    and blocked_accept(path, relative)
                ):
                    result.blocked_error = error
                continue
            if accept is not None and not accept(path, relative):
                continue
            result.files.append(path)
            if max_files is not None and len(result.files) >= max_files:
                result.truncated_reason = f"result limit ({max_files}) reached"
                return result

        stack.extend((path, current_depth + 1) for path in reversed(child_directories))

    return result
