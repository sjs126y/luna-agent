"""grep — content search with regex over files within sandbox."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from personal_agent.tools.entry import ToolEntry, ToolHandlerOutput
from personal_agent.tools.registry import tool_registry
from personal_agent.tools.sandbox import get_sandbox

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

    # Build glob filter
    glob_parts = glob.split(",") if glob else ["*"]

    results: list[str] = []
    file_count = 0
    match_count = 0
    blocked_error = ""

    try:
        for f in sorted(search_dir.rglob("*")):
            if not f.is_file():
                continue
            if f.stat().st_size > _MAX_FILE_SIZE:
                continue
            # Check glob filters
            rel = str(f.relative_to(search_dir))
            if not any(fnmatch.fnmatch(rel, g.strip()) for g in glob_parts):
                continue
            candidate_error = sandbox.check_path(f)
            if candidate_error:
                if "path blocked by sandbox" in candidate_error.lower():
                    blocked_error = blocked_error or candidate_error
                continue
            if any(p.startswith(".") for p in f.parts):  # skip hidden
                continue
            if any(p in ("node_modules", "__pycache__", ".venv", ".git")
                   for p in f.parts):
                continue

            file_count += 1
            try:
                for lineno, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if regex.search(line):
                        match_count += 1
                        if match_count > _MAX_MATCHES:
                            results.append(f"...({match_count - _MAX_MATCHES} more matches truncated)")
                            return "\n".join(results)
                        if output_mode == "content":
                            results.append(f"{rel}:{lineno}: {line[:200]}")
                        elif output_mode == "files_with_matches":
                            if rel not in results:
                                results.append(rel)
                        elif output_mode == "count":
                            pass  # handled below
            except Exception:
                continue

        if not file_count and blocked_error:
            return ToolHandlerOutput(
                text=blocked_error,
                is_error=True,
                metadata={"reason_code": "sandbox_blocked"},
            )
        if output_mode == "count":
            results.append(f"{match_count} matches in {file_count} files")
        elif not results:
            results.append(f"No matches for '{pattern}' in {path}")
    except Exception as e:
        return f"Error: {e}"

    if head_limit and len(results) > head_limit:
        results = results[:head_limit]
        results.append(f"...(truncated, {match_count - head_limit} more)")
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
))
