"""Read bounded text windows within sandbox boundaries.

Security is enforced by the unified sandbox (roots + blocked patterns).
"""

import asyncio

from personal_agent.plugins.builtin.tools.builtin.file_io import read_text_window
from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry
from personal_agent.tools.sandbox import get_sandbox

MAX_READ_BYTES = 50_000
MAX_SCAN_BYTES = 5_000_000
DEFAULT_LINE_LIMIT = 500
MAX_LINE_LIMIT = 2_000


async def _file_read(path: str, offset: int = 1, limit: int = DEFAULT_LINE_LIMIT) -> str:
    try:
        offset = int(offset)
        limit = int(limit)
        if offset < 1:
            return "Error: offset must be at least 1"
        if limit < 1 or limit > MAX_LINE_LIMIT:
            return f"Error: limit must be between 1 and {MAX_LINE_LIMIT}"
        sandbox = get_sandbox()
        full = sandbox.resolve(path)
        error = sandbox.check_path(full)
        if error:
            return error

        if not full.exists():
            return f"Error: file not found: {path}"
        if full.is_dir():
            return f"Error: '{path}' is a directory"
        window = await asyncio.to_thread(
            read_text_window,
            full,
            offset=offset,
            limit=limit,
            max_bytes=MAX_READ_BYTES,
            max_scan_bytes=MAX_SCAN_BYTES,
        )
        if window.error:
            return window.error
        if window.next_offset is None:
            return window.text
        return (
            window.text
            + f"\n\n...(truncated; continue with offset={window.next_offset})"
        )
    except Exception as e:
        return f"Error: {e}"


tool_registry.register(ToolEntry(
    name="read",
    description="Read a file from the agent's allowed directories. Accepts relative or absolute paths.",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to file, e.g. 'notes/ideas.txt' or 'C:/Users/.../file.md'"},
            "offset": {"type": "integer", "minimum": 1, "description": "1-based starting line, default 1"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LINE_LIMIT, "description": "Maximum lines to return, default 500"},
        },
        "required": ["path"],
    },
    handler=_file_read,
    toolset="builtin",
    permission_category="read",
    tags=["file", "read"],
    risk_level="low",
    usage_hint="Use to inspect a known file path before editing or summarizing it.",
    timeout_seconds=8,
))
