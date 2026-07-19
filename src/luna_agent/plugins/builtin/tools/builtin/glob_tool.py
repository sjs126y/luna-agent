"""glob — file pattern matching within sandbox."""

from __future__ import annotations

import asyncio
import glob as glob_module
from pathlib import Path

from luna_agent.plugins.builtin.tools.builtin.file_scan import scan_files
from luna_agent.tools.entry import ToolEntry, ToolHandlerOutput
from luna_agent.tools.registry import tool_registry
from luna_agent.tools.sandbox import get_sandbox

_MAX_RESULTS = 100


async def _glob(
    pattern: str,
    path: str = ".",
    max_results: int = _MAX_RESULTS,
    max_depth: int | None = None,
    include_hidden: bool = False,
) -> str:
    """Find files matching glob pattern."""
    sandbox = get_sandbox()
    search_dir = sandbox.resolve(path)
    error = sandbox.check_path(search_dir)
    if error:
        return error

    if not search_dir.exists():
        return f"Error: path not found: {path}"
    if not search_dir.is_dir():
        return f"Error: '{path}' is not a directory"

    try:
        limit = max(1, min(int(max_results), 500))
    except (TypeError, ValueError):
        return "Error: max_results must be an integer"
    if max_depth is not None:
        try:
            max_depth = int(max_depth)
        except (TypeError, ValueError):
            return "Error: max_depth must be an integer"
        if max_depth < 1 or max_depth > 100:
            return "Error: max_depth must be between 1 and 100"

    explicit_name = not glob_module.has_magic(Path(pattern).name)

    def matches(candidate: Path, relative: str) -> bool:
        return candidate.match(pattern)

    def explicitly_matches_blocked(candidate: Path, relative: str) -> bool:
        return explicit_name and candidate.match(pattern)

    try:
        scan = await asyncio.to_thread(
            scan_files,
            search_dir,
            sandbox,
            accept=matches,
            blocked_accept=explicitly_matches_blocked,
            max_files=limit,
            max_depth=max_depth,
            include_hidden=bool(include_hidden),
        )
    except Exception as e:
        return f"Error: {e}"

    if not scan.files and scan.blocked_error:
        return ToolHandlerOutput(
            text=scan.blocked_error,
            is_error=True,
            metadata={"reason_code": "sandbox_blocked"},
        )
    if not scan.files:
        return f"No files matching '{pattern}' in {path}"
    results = [candidate.relative_to(search_dir).as_posix() for candidate in scan.files]
    if scan.truncated_reason:
        results.append(
            f"...(truncated: {scan.truncated_reason}; "
            f"scanned {scan.scanned_entries} entries)"
        )
    return "\n".join(results)


tool_registry.register(ToolEntry(
    name="glob",
    description="Find files matching a glob pattern. Supports *, **, ?, [chars].",
    schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py' or '*.md'"},
            "path": {"type": "string", "description": "Directory to search in (relative or absolute), default '.'"},
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "description": "Maximum matching files to return, default 100",
            },
            "max_depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Maximum path depth below the search directory; 1 means immediate files only",
            },
            "include_hidden": {
                "type": "boolean",
                "description": "Include hidden files and ordinary hidden directories, default false",
            },
        },
        "required": ["pattern"],
    },
    handler=_glob,
    toolset="builtin",
    permission_category="read",
    tags=["file", "search", "read"],
    risk_level="low",
    usage_hint="Use to find files by path pattern before reading or editing them.",
    timeout_seconds=12,
))
