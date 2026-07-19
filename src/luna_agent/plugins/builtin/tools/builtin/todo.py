"""Todo list — CC-style, in-memory, session-scoped.

Pass the FULL list every call (replaces previous state). No persistence —
this tracks what the agent is doing right now. For durable cross-session
tasks, use the cron/task system instead.

Pattern:
  - One array → full state → one call
  - Only ONE item in_progress at a time
  - List order = priority (higher = more important)
  - Mark completed IMMEDIATELY when done
  - Cancel failed items + add revised item
  - activeForm: present-tense label shown while working (e.g. "Fixing login")
"""

from __future__ import annotations

import json

from luna_agent.tools.entry import ToolEntry
from luna_agent.tools.registry import tool_registry

# In-memory state — one per process, lost on restart
_items: list[dict] = []
_MAX_ITEMS = 100


def _format(items: list[dict]) -> str:
    if not items:
        return "No todos."

    lines = []
    for i, t in enumerate(items):
        content = t.get("content", "")
        status = t.get("status", "pending")
        active = t.get("activeForm", "")

        if status == "in_progress":
            lines.append(f"  [{i}] {active or content}")
        elif status == "completed":
            lines.append(f"  [{i}] ~~{content}~~")
        elif status == "cancelled":
            lines.append(f"  [{i}] ~~{content}~~ (cancelled)")
        else:
            lines.append(f"  [{i}] {content}")

    counts = {
        "total": len(items),
        "pending": sum(1 for t in items if t.get("status") == "pending"),
        "in_progress": sum(1 for t in items if t.get("status") == "in_progress"),
        "completed": sum(1 for t in items if t.get("status") == "completed"),
    }
    header = (
        f"{counts['total']} todos ({counts['pending']} pending"
        + (f", {counts['in_progress']} in progress" if counts["in_progress"] else "")
        + (f", {counts['completed']} done" if counts["completed"] else "")
        + "):"
    )
    return header + "\n" + "\n".join(lines)


async def _todo(todos: str = "[]") -> str:
    """Manage your task list. Pass the FULL list every call.

    Args:
        todos: JSON string of ALL todo items.
            [{"content": "...", "status": "pending|in_progress|completed|cancelled",
              "activeForm": "present-tense label while in progress"}]

    Rules:
        - List order = priority (most important first)
        - Only ONE item in_progress at a time
        - Mark done immediately, don't batch
        - Cancel failed items, add a revised one
        - Remove items no longer relevant
        - Max {_MAX_ITEMS} items
    """
    global _items

    try:
        new_items = json.loads(todos)
    except json.JSONDecodeError as e:
        return f"Error: invalid todos JSON: {e}"

    if not isinstance(new_items, list):
        return "Error: todos must be a JSON array"

    # ── Validate ──
    in_progress = [t for t in new_items if t.get("status") == "in_progress"]
    if len(in_progress) > 1:
        return (
            f"Error: {len(in_progress)} items marked in_progress. "
            f"Only ONE at a time. Mark the others as pending."
        )

    if len(new_items) > _MAX_ITEMS:
        return f"Error: too many items ({len(new_items)}, max {_MAX_ITEMS})"

    # ── Normalize ──
    cleaned = []
    for t in new_items:
        if not isinstance(t, dict):
            continue
        content = str(t.get("content", "")).strip()
        if not content:
            continue
        status = t.get("status", "pending")
        if status not in ("pending", "in_progress", "completed", "cancelled"):
            status = "pending"
        active = str(t.get("activeForm", ""))[:200]
        cleaned.append({"content": content, "status": status, "activeForm": active})

    _items = cleaned
    return _format(_items)


tool_registry.register(ToolEntry(
    name="todo",
    description=(
        "Track your current task list. Send the FULL list every call — "
        "this replaces the previous state. List order is priority. "
        "Only ONE item in_progress at a time. Mark completed immediately. "
        "Cancel failed items and add revised ones. "
        "Each item: {content, status, activeForm}. "
        "status: pending|in_progress|completed|cancelled. "
        "activeForm: short present-tense label while working on it."
    ),
    schema={
        "type": "object",
        "properties": {
            "todos": {
                "type": "string",
                "description": (
                    "JSON array of ALL todo items. Pass the complete list — "
                    "existing items will be replaced. "
                    "[{content: string, status: pending|in_progress|completed|cancelled, "
                    "activeForm: string}]"
                ),
            },
        },
        "required": ["todos"],
    },
    handler=_todo,
    toolset="builtin",
))
