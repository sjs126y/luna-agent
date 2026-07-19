"""grep — content search with regex over files within sandbox."""

from __future__ import annotations

import asyncio
import fnmatch
import glob as glob_module
import re
from pathlib import Path

from luna_agent.plugins.builtin.tools.builtin.file_scan import scan_files
from luna_agent.tools.entry import ToolEntry, ToolHandlerOutput
from luna_agent.tools.registry import tool_registry
from luna_agent.tools.sandbox import get_sandbox

_MAX_MATCHES = 50
_MAX_FILE_SIZE = 500_000  # skip files > 500KB


async def _grep(
    pattern: str,
    path: str = ".",
    glob: str = "",
    output_mode: str = "content",
    head_limit: int = 40,
    literal: bool = False,
) -> str:
    """Search file contents with regex."""
    search_pattern = re.escape(pattern) if literal else pattern
    try:
        regex = re.compile(search_pattern)
    except re.error as e:
        return f"Error: invalid regex pattern: {e}"

    sandbox = get_sandbox()
    search_dir = sandbox.resolve(path)
    error = sandbox.check_path(search_dir)
    if error:
        return error

    if not search_dir.exists():
        return f"Error: path not found: {path}"
    if not search_dir.is_dir():
        return f"Error: '{path}' is not a directory"

    # Build glob filter
    glob_parts = glob.split(",") if glob else ["*"]

    try:
        return await asyncio.to_thread(
            _grep_sync,
            regex,
            search_dir,
            sandbox,
            glob_parts,
            output_mode,
            head_limit,
            pattern,
            path,
        )
    except Exception as e:
        return f"Error: {e}"


def _grep_sync(
    regex: re.Pattern[str],
    search_dir: Path,
    sandbox,
    glob_parts: list[str],
    output_mode: str,
    head_limit: int,
    original_pattern: str,
    original_path: str,
) -> str | ToolHandlerOutput:
    results: list[str] = []
    matched_files: set[str] = set()
    match_count = 0
    file_count = 0
    output_limit = max(1, min(int(head_limit or 40), _MAX_MATCHES))
    match_limit_reached = False

    def inspect_file(candidate: Path, relative: str) -> bool:
        nonlocal file_count, match_count, match_limit_reached
        try:
            if candidate.stat().st_size > _MAX_FILE_SIZE:
                return False
            if not any(fnmatch.fnmatch(relative, item.strip()) for item in glob_parts):
                return False
            with candidate.open("r", encoding="utf-8", errors="replace") as handle:
                sample = handle.read(8192)
                if "\x00" in sample:
                    return False
                handle.seek(0)
                file_count += 1
                for lineno, line in enumerate(handle, 1):
                    if not regex.search(line):
                        continue
                    match_count += 1
                    matched_files.add(relative)
                    if output_mode == "content" and len(results) < output_limit:
                        results.append(f"{relative}:{lineno}: {line.rstrip()[:200]}")
                    elif (
                        output_mode == "files_with_matches"
                        and relative not in results
                        and len(results) < output_limit
                    ):
                        results.append(relative)
                    if match_count >= _MAX_MATCHES:
                        match_limit_reached = True
                        return True
        except (OSError, UnicodeError):
            return False
        return False

    scan = scan_files(
        search_dir,
        sandbox,
        accept=inspect_file,
        blocked_accept=lambda candidate, relative: any(
            not glob_module.has_magic(Path(item.strip()).name)
            and fnmatch.fnmatch(relative, item.strip())
            for item in glob_parts
        ),
        max_files=1,
    )
    truncated_reason = scan.truncated_reason
    if match_limit_reached:
        truncated_reason = f"match limit ({_MAX_MATCHES}) reached"

    if not file_count and scan.blocked_error:
        return ToolHandlerOutput(
            text=scan.blocked_error,
            is_error=True,
            metadata={"reason_code": "sandbox_blocked"},
        )
    if output_mode == "count":
        results = [f"{match_count} matches in {len(matched_files)} files ({file_count} searched)"]
    elif not results:
        results = [f"No matches for '{original_pattern}' in {original_path}"]
    if truncated_reason:
        results.append(
            f"...(partial results: {truncated_reason}; "
            f"scanned {scan.scanned_entries} entries)"
        )
    return "\n".join(results)


tool_registry.register(ToolEntry(
    name="grep",
    description="Search file contents with regex. Returns matching lines with file:lineno prefix.",
    schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to search for"},
            "path": {"type": "string", "description": "Directory to search (relative or absolute), default '.'"},
            "glob": {"type": "string", "description": "Comma-separated glob patterns to filter files, e.g. '*.py,*.ts'"},
            "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"],
                           "description": "content: matching lines, files_with_matches: file paths, count: match counts"},
            "head_limit": {"type": "integer", "description": "Max output lines (default 40)"},
            "literal": {"type": "boolean", "description": "Treat pattern as plain text instead of regex"},
        },
        "required": ["pattern"],
    },
    handler=_grep,
    toolset="builtin",
    permission_category="read",
    tags=["file", "search", "read"],
    risk_level="low",
    usage_hint="Use to search file contents by regex before opening or editing matching files.",
    timeout_seconds=12,
))
