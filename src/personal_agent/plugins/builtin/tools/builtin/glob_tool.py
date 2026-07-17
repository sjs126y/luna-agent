"""glob — file pattern matching within sandbox."""

from __future__ import annotations

from pathlib import Path

from personal_agent.tools.entry import ToolEntry, ToolHandlerOutput
from personal_agent.tools.registry import tool_registry
from personal_agent.tools.sandbox import get_sandbox

_MAX_RESULTS = 100


async def _glob(pattern: str, path: str = ".") -> str:
    """Find files matching glob pattern."""
    sandbox = get_sandbox()
    search_dir = sandbox.resolve(path)
    error = sandbox.check_path(search_dir)
    if error:
        return error

    if not search_dir.exists():
        return f"Error: path not found: {path}"

    results = []
    blocked_error = ""
    allowed_candidates = 0
    try:
        for f in sorted(search_dir.rglob(pattern)):
            if not f.is_file():
                continue
            candidate_error = sandbox.check_path(f)
            if candidate_error:
                if "path blocked by sandbox" in candidate_error.lower():
                    blocked_error = blocked_error or candidate_error
                continue
            if any(p.startswith(".") for p in f.parts):
                continue
            if any(p in ("node_modules", "__pycache__", ".venv", ".git")
                   for p in f.parts):
                continue
            allowed_candidates += 1
            rel = str(f.relative_to(search_dir))
            results.append(rel)
            if len(results) >= _MAX_RESULTS:
                results.append(f"...({len(results) - _MAX_RESULTS + 1} more files)")
                break
    except Exception as e:
        return f"Error: {e}"

    if not results and blocked_error and not allowed_candidates:
        return ToolHandlerOutput(
            text=blocked_error,
            is_error=True,
            metadata={"reason_code": "sandbox_blocked"},
        )
    if not results:
        return f"No files matching '{pattern}' in {path}"
    return "\n".join(results)


tool_registry.register(ToolEntry(
    name="glob",
    description="Find files matching a glob pattern. Supports *, **, ?, [chars].",
    schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py' or '*.md'"},
            "path": {"type": "string", "description": "Directory to search in (relative or absolute), default '.'"},
        },
        "required": ["pattern"],
    },
    handler=_glob,
    toolset="builtin",
    permission_category="read",
    tags=["file", "search", "read"],
    risk_level="low",
    usage_hint="Use to find files by path pattern before reading or editing them.",
))
