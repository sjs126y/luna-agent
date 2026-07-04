"""Read files within sandbox boundaries.

Security is enforced by the unified sandbox (roots + blocked patterns).
"""

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry
from personal_agent.tools.sandbox import get_sandbox

MAX_READ_BYTES = 50_000


async def _file_read(path: str) -> str:
    try:
        sandbox = get_sandbox()
        full = sandbox.resolve(path)
        error = sandbox.check_path(full)
        if error:
            return error

        if not full.exists():
            return f"Error: file not found: {path}"
        if full.is_dir():
            return f"Error: '{path}' is a directory"
        content = full.read_text(encoding="utf-8", errors="replace")
        if len(content) > MAX_READ_BYTES:
            content = content[:MAX_READ_BYTES] + f"\n\n...(truncated {len(content) - MAX_READ_BYTES} bytes)"
        return content
    except Exception as e:
        return f"Error: {e}"


tool_registry.register(ToolEntry(
    name="read",
    description="Read a file from the agent's allowed directories. Accepts relative or absolute paths.",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to file, e.g. 'notes/ideas.txt' or 'C:/Users/.../file.md'"},
        },
        "required": ["path"],
    },
    handler=_file_read,
    toolset="builtin",
    permission_category="read",
    tags=["file", "read"],
    risk_level="low",
    usage_hint="Use to inspect a known file path before editing or summarizing it.",
))
