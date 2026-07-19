"""Atomic JSON file helpers for small runtime state files."""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def read_json(path: Path | str, default: Any) -> Any:
    target = Path(path)
    if not target.exists():
        return _clone_default(default)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        backup = backup_corrupt_file(target)
        logger.exception("Failed to read JSON state: %s (backup=%s)", target, backup or "-")
        return _clone_default(default)


def read_json_object(path: Path | str, default: dict[str, Any]) -> dict[str, Any]:
    target = Path(path)
    data = read_json(target, default)
    if isinstance(data, dict):
        return data
    backup = backup_corrupt_file(target)
    logger.error("JSON state is not an object: %s (backup=%s)", target, backup or "-")
    return _clone_default(default)


def write_json_atomic(
    path: Path | str,
    data: Any,
    *,
    indent: int | None = 2,
    ensure_ascii: bool = False,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp_name).replace(target)
        _fsync_dir(target.parent)
    except Exception:
        if tmp_name:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except Exception:
                pass
        raise


def backup_corrupt_file(path: Path | str) -> str:
    target = Path(path)
    if not target.exists():
        return ""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = target.with_name(f"{target.name}.corrupt.{stamp}")
    counter = 1
    while backup.exists():
        backup = target.with_name(f"{target.name}.corrupt.{stamp}.{counter}")
        counter += 1
    try:
        shutil.copy2(target, backup)
    except Exception:
        logger.exception("Failed to back up corrupt JSON file: %s", target)
        return ""
    return str(backup)


def _clone_default(default: Any) -> Any:
    return copy.deepcopy(default)


def _fsync_dir(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except Exception:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
