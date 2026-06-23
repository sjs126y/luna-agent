"""Edit files within sandbox boundaries — append or replace text."""

from pathlib import Path

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry
from personal_agent.tools.sandbox import get_sandbox

_MAX_WRITE_BYTES = 100_000


async def _file_edit(action: str, path: str, content: str = "",
                     old_text: str = "", new_text: str = "") -> str:
    try:
        sandbox = get_sandbox()
        full = sandbox.resolve(path)
        error = sandbox.check_path(full)
        if error:
            return error

        if action == "append":
            full.parent.mkdir(parents=True, exist_ok=True)
            existing = full.read_text(encoding="utf-8") if full.exists() else ""
            if len(existing) + len(content) > _MAX_WRITE_BYTES:
                return f"Error: file would exceed max size ({_MAX_WRITE_BYTES // 1000}KB)"
            full.write_text(existing + content, encoding="utf-8")
            msg = f"Appended {len(content)} bytes to {path}"
            _audit(msg, True)
            return msg

        elif action == "replace":
            if not full.exists():
                return f"Error: file not found: {path}"
            text = full.read_text(encoding="utf-8")
            if old_text not in text:
                return f"Error: old_text not found in {path}"
            new_text_full = text.replace(old_text, new_text, 1)
            if len(new_text_full) > _MAX_WRITE_BYTES:
                return f"Error: result would exceed max size ({_MAX_WRITE_BYTES // 1000}KB)"
            full.write_text(new_text_full, encoding="utf-8")
            msg = f"Replaced 1 occurrence in {path}"
            _audit(msg, True)
            return msg

        return f"Error: unknown action '{action}'. Use 'append' or 'replace'."
    except Exception as e:
        _audit(f"Error: {e}", False)
        return f"Error: {e}"


def _audit(msg: str, success: bool) -> None:
    try:
        from personal_agent.tools.audit import audit_log
        audit_log("file_edit", msg[:200], msg[:200], success)
    except Exception:
        pass


tool_registry.register(ToolEntry(
    name="edit",
    description="Edit a file by appending content or replacing text. "
                "Actions: 'append' (add to end), 'replace' (find and replace first occurrence).",
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["append", "replace"],
                       "description": "append: add content to end. replace: find old_text and replace with new_text."},
            "path": {"type": "string", "description": "Path to file (relative or absolute)"},
            "content": {"type": "string", "description": "Content to append (for append action)"},
            "old_text": {"type": "string", "description": "Text to find (for replace action)"},
            "new_text": {"type": "string", "description": "Replacement text (for replace action)"},
        },
        "required": ["action", "path"],
    },
    handler=_file_edit,
    toolset="builtin",
    is_parallel_safe=False,
    is_destructive=True,
))
