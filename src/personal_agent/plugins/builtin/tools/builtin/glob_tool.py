"""glob — file pattern matching within sandbox."""

from __future__ import annotations

import asyncio
from pathlib import Path

from personal_agent.plugins.builtin.tools.builtin.file_scan import scan_files
from personal_agent.tools.entry import ToolEntry, ToolHandlerOutput
from personal_agent.tools.registry import tool_registry
from personal_agent.tools.sandbox import get_sandbox

_MAX_RESULTS = 100


async def _glob(pattern: str, path: str = ".", max_results: int = _MAX_RESULTS) -> str:
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

    try:
        scan = await asyncio.to_thread(
            scan_files,
            search_dir,
            sandbox,
            accept=lambda candidate, relative: candidate.match(pattern),
            max_files=limit,
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
