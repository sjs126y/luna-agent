"""Edit files within sandbox boundaries — append or replace text."""

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry
from personal_agent.tools.sandbox import get_sandbox

_MAX_WRITE_BYTES = 100_000


async def _file_edit(action: str, path: str, content: str = "",
                     old_text: str = "", new_text: str = "") -> str:
    try:
        sandbox = get_sandbox()
        full = sandbox.resolve(path)
        error = sandbox.check_path(full, access="write")
        if error:
            return error

        if action == "append":
            if content == "":
                return "Error: append content cannot be empty"
            full.parent.mkdir(parents=True, exist_ok=True)
            existing = full.read_text(encoding="utf-8") if full.exists() else ""
            if len(existing) + len(content) > _MAX_WRITE_BYTES:
                return f"Error: file would exceed max size ({_MAX_WRITE_BYTES // 1000}KB)"
            full.write_text(existing + content, encoding="utf-8")
            msg = (
                f"Appended {len(content)} chars to {path} "
                f"({len(existing)} -> {len(existing) + len(content)} chars)"
            )
            return msg

        elif action in {"replace", "replace_all"}:
            if old_text == "":
                return f"Error: old_text cannot be empty for {action}"
            if not full.exists():
                return f"Error: file not found: {path}"
            text = full.read_text(encoding="utf-8")
            occurrences = text.count(old_text)
            if occurrences == 0:
                return f"Error: old_text not found in {path} (occurrences=0)"
            replace_count = occurrences if action == "replace_all" else 1
            new_text_full = text.replace(old_text, new_text, replace_count)
            if len(new_text_full) > _MAX_WRITE_BYTES:
                return f"Error: result would exceed max size ({_MAX_WRITE_BYTES // 1000}KB)"
            full.write_text(new_text_full, encoding="utf-8")
            if action == "replace_all":
                msg = (
                    f"Replaced all {occurrences} occurrences in {path} "
                    f"({len(text)} -> {len(new_text_full)} chars)"
                )
            elif occurrences == 1:
                msg = (
                    f"Replaced 1 occurrence in {path} "
                    f"({len(text)} -> {len(new_text_full)} chars)"
                )
            else:
                msg = (
                    f"Replaced 1 of {occurrences} occurrences in {path} "
                    f"({len(text)} -> {len(new_text_full)} chars). "
                    "Use a more specific old_text or action='replace_all' for all matches."
                )
            return msg

        return f"Error: unknown action '{action}'. Use 'append', 'replace', or 'replace_all'."
    except Exception as e:
        return f"Error: {e}"


tool_registry.register(ToolEntry(
    name="edit",
    description="Edit a file in the agent's allowed directories by appending non-empty content, "
                "replacing the first occurrence of old_text, or replacing all occurrences. "
                "Reports occurrence counts and size changes.",
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["append", "replace", "replace_all"],
                       "description": "append: add content to end. replace: replace first old_text. replace_all: replace every old_text."},
            "path": {"type": "string", "description": "Path to file (relative or absolute)"},
            "content": {"type": "string", "description": "Content to append (for append action)"},
            "old_text": {"type": "string", "description": "Text to find (for replace action)"},
            "new_text": {"type": "string", "description": "Replacement text (for replace action)"},
        },
        "required": ["action", "path"],
    },
    handler=_file_edit,
    toolset="builtin",
    permission_category="write",
    tags=["file", "write", "edit"],
    risk_level="high",
    usage_hint="Use for small append or replacement edits after reading the target file; prefer replace for unique old_text and replace_all for intentional bulk changes.",
    is_parallel_safe=False,
    is_destructive=True,
))
