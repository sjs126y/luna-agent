"""glob — file pattern matching in workspace."""

from __future__ import annotations

import fnmatch
from pathlib import Path

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry

_workspace: Path = Path("./data").resolve()
_MAX_RESULTS = 100


def set_workspace(path: Path) -> None:
    global _workspace
    _workspace = path.resolve()


async def _glob(pattern: str, path: str = ".") -> str:
    """Find files matching glob pattern."""
    search_dir = (_workspace / path).resolve()
    if not str(search_dir).startswith(str(_workspace)):
        return f"Error: path '{path}' is outside workspace"

    if not search_dir.exists():
        return f"Error: path not found: {path}"

    results = []
    try:
        for f in sorted(search_dir.rglob(pattern)):
            if not f.is_file():
                continue
            if any(p.startswith(".") for p in f.parts):
                continue
            if any(p in ("node_modules", "__pycache__", ".venv", ".git")
                   for p in f.parts):
                continue
            rel = str(f.relative_to(search_dir))
            results.append(rel)
            if len(results) >= _MAX_RESULTS:
                results.append(f"...({len(results) - _MAX_RESULTS + 1} more files)")
                break
    except Exception as e:
        return f"Error: {e}"

    if not results:
        return f"No files matching '{pattern}' in {path}"
    return "\n".join(results)


tool_registry.register(ToolEntry(
    name="glob",
    description="Find files matching a glob pattern. Like 'ls **/*.py' but faster. "
                "Returns relative file paths sorted by modification time.",
    schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py' or 'src/**/*.ts'"},
            "path": {"type": "string", "description": "Directory to search, relative to workspace. Default '.'"},
        },
        "required": ["pattern"],
    },
    handler=_glob,
    toolset="builtin",
    is_parallel_safe=True,
))
