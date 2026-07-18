"""Bounded and atomic primitives for built-in file tools."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile


@dataclass(frozen=True)
class TextWindow:
    text: str
    next_offset: int | None = None
    error: str = ""


def read_text_window(
    path: Path,
    *,
    offset: int,
    limit: int,
    max_bytes: int,
    max_scan_bytes: int,
) -> TextWindow:
    """Read a bounded line window without loading the whole file."""
    scanned_bytes = 0
    selected: list[bytes] = []
    selected_bytes = 0
    selected_lines = 0
    current_line = 0
    truncated = False

    with path.open("rb") as handle:
        sample = handle.read(8192)
        if b"\x00" in sample:
            return TextWindow("", error="Error: binary files are not supported by read")
        handle.seek(0)

        for raw_line in handle:
            current_line += 1
            scanned_bytes += len(raw_line)
            if current_line < offset:
                if scanned_bytes > max_scan_bytes:
                    return TextWindow(
                        "",
                        error=(
                            f"Error: offset {offset} requires scanning more than "
                            f"{max_scan_bytes} bytes"
                        ),
                    )
                continue
            if selected_lines >= limit:
                truncated = True
                break

            remaining = max_bytes - selected_bytes
            if remaining <= 0:
                truncated = True
                break
            if len(raw_line) > remaining:
                if not selected:
                    return TextWindow(
                        "",
                        error=(
                            f"Error: line {current_line} exceeds the "
                            f"{max_bytes}-byte read window"
                        ),
                    )
                truncated = True
                break
            selected.append(raw_line)
            selected_bytes += len(raw_line)
            selected_lines += 1

    text = b"".join(selected).decode("utf-8", errors="replace")
    next_offset = current_line if truncated else None
    return TextWindow(text=text, next_offset=next_offset)


def atomic_write_text(path: Path, content: str) -> None:
    """Write UTF-8 content atomically in the target directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_mode = path.stat().st_mode if path.exists() else None
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        if previous_mode is not None:
            os.chmod(temporary_path, previous_mode)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def utf8_size(value: str) -> int:
    return len(str(value).encode("utf-8"))
