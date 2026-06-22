"""Write files within allowed data directory — destructive tool."""

from pathlib import Path

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry

# Shared with file_read — set at startup
_allowed_base: Path = Path("./data")


def set_allowed_base(path: Path) -> None:
    global _allowed_base
    _allowed_base = path.resolve()


async def _file_write(path: str, content: str) -> str:
    try:
        full = (_allowed_base / path).resolve()
        if not str(full).startswith(str(_allowed_base)):
            return f"Error: path traversal denied — '{path}' is outside allowed directory"
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        return f"Written {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


tool_registry.register(ToolEntry(
    name="file_write",
    description="Write content to a file in the agent's data directory. Path is relative. Creates parent directories as needed.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path to file, e.g. 'output/report.md'"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["path", "content"],
    },
    handler=_file_write,
    toolset="builtin",
    is_parallel_safe=False,
    is_destructive=True,
))
