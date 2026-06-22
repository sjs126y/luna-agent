"""Todo list management — persisted to data/todos.json."""

import json
import time
from pathlib import Path

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry

_todos_path: Path = Path("./data/todos.json")


def set_todos_path(path: Path) -> None:
    global _todos_path
    _todos_path = path


def _load() -> list[dict]:
    if not _todos_path.exists():
        return []
    return json.loads(_todos_path.read_text())


def _save(todos: list[dict]) -> None:
    _todos_path.parent.mkdir(parents=True, exist_ok=True)
    _todos_path.write_text(json.dumps(todos, indent=2, ensure_ascii=False))


async def _todo(action: str, title: str = "", id: int = 0, status: str = "pending") -> str:
    todos = _load()
    if action == "add":
        new_id = max((t.get("id", 0) for t in todos), default=0) + 1
        todos.append({
            "id": new_id, "title": title, "status": "pending",
            "created_at": time.time(),
        })
        _save(todos)
        return f"Todo #{new_id} added: {title}"

    if action == "list":
        if not todos:
            return "No todos."
        lines = []
        for t in sorted(todos, key=lambda x: x.get("id", 0)):
            status_mark = "[x]" if t.get("status") == "done" else "[ ]"
            lines.append(f"#{t['id']} {status_mark} {t.get('title', '')}")
        return "\n".join(lines)

    if action == "update":
        for t in todos:
            if t.get("id") == id:
                if title:
                    t["title"] = title
                if status in ("pending", "done", "cancelled"):
                    t["status"] = status
                _save(todos)
                return f"Todo #{id} updated: {t['title']} [{t['status']}]"
        return f"Todo #{id} not found."

    if action == "delete":
        before = len(todos)
        todos = [t for t in todos if t.get("id") != id]
        if len(todos) < before:
            _save(todos)
            return f"Todo #{id} deleted."
        return f"Todo #{id} not found."

    return f"Unknown action: {action}"


tool_registry.register(ToolEntry(
    name="todo",
    description="Manage a todo list. Actions: add (create), list (show all), update (modify title/status), delete (remove). Status can be 'pending', 'done', or 'cancelled'.",
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "list", "update", "delete"],
                "description": "Action to perform",
            },
            "title": {"type": "string", "description": "Todo title (for add/update)"},
            "id": {"type": "integer", "description": "Todo ID (for update/delete)"},
            "status": {"type": "string", "description": "New status (for update): pending, done, cancelled"},
        },
        "required": ["action"],
    },
    handler=_todo,
    toolset="builtin",
))
