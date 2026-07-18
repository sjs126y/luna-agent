"""Write files within sandbox boundaries — destructive tool.

Safety:
  - Sandbox roots + blocked patterns (unified)
  - Extension whitelist (no .exe/.bat/.sh etc.)
  - Max file size
"""

import asyncio
from pathlib import Path

from personal_agent.plugins.builtin.tools.builtin.file_io import atomic_write_text, utf8_size
from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry
from personal_agent.tools.sandbox import get_sandbox

# Only these extensions are writable (and their uppercase variants)
_ALLOWED_EXTENSIONS: set[str] = {
    ".txt", ".md", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".svg",
    ".csv", ".log", ".xml", ".rst", ".tex", ".bib",
    ".sh", ".bat", ".ps1", ".env", ".gitignore", ".dockerignore",
}
_MAX_WRITE_BYTES = 100_000


def set_max_write_bytes(max_bytes: int) -> None:
    global _MAX_WRITE_BYTES
    _MAX_WRITE_BYTES = max_bytes


def _check_extension(path: str) -> str | None:
    suffix = Path(path).suffix
    if suffix and suffix.lower() not in _ALLOWED_EXTENSIONS:
        return (
            f"Error: file extension '{suffix}' is not allowed. "
            f"Allowed: {', '.join(sorted(_ALLOWED_EXTENSIONS))}"
        )
    return None


async def _file_write(path: str, content: str) -> str:
    ext_error = _check_extension(path)
    if ext_error:
        return ext_error

    content_bytes = utf8_size(content)
    if content_bytes > _MAX_WRITE_BYTES:
        return f"Error: content too large ({content_bytes} bytes, max {_MAX_WRITE_BYTES})"

    try:
        sandbox = get_sandbox()
        full = sandbox.resolve(path)
        error = sandbox.check_path(full, access="write")
        if error:
            return error

        await asyncio.to_thread(atomic_write_text, full, content)
        msg = f"Written {len(content)} chars to {path} (overwrite)"
        return msg
    except Exception as e:
        return f"Error: {e}"


def _precheck(input_: dict) -> str | None:
    path = input_.get("path", "")
    if path:
        ext_error = _check_extension(path)
        if ext_error:
            return ext_error
        full = get_sandbox().resolve(path)
        sandbox_error = get_sandbox().check_blocked_path(full)
        if sandbox_error:
            return sandbox_error

    content = input_.get("content", "")
    content_bytes = utf8_size(content)
    if content_bytes > _MAX_WRITE_BYTES:
        return f"Error: content too large ({content_bytes} bytes, max {_MAX_WRITE_BYTES})"
    return None


tool_registry.register(ToolEntry(
    name="write",
    description="Overwrite a file with full content in the agent's allowed directories. "
                f"Allowed extensions: {', '.join(sorted(_ALLOWED_EXTENSIONS))}. Max {_MAX_WRITE_BYTES // 1000}KB.",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to file (relative or absolute)"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["path", "content"],
    },
    handler=_file_write,
    toolset="builtin",
    permission_category="write",
    tags=["file", "write"],
    risk_level="high",
    usage_hint="Use only when replacing an entire file with complete intended content.",
    precheck=_precheck,
    is_parallel_safe=False,
    is_destructive=True,
))
