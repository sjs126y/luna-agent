"""Tool handlers delegating all memory operations to MemoryManager."""

from __future__ import annotations

import json

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.runtime_context import current_tool_agent

_manager = None


def set_memory_manager(manager) -> None:
    global _manager
    _manager = manager


def memory_tool_entry() -> ToolEntry:
    return ToolEntry(
        name="memory",
        description="Manage external long-term memory: add, search, list, delete, or history.",
        schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "search", "list", "delete", "history"]},
                "content": {"type": "string"},
                "query": {"type": "string"},
                "memory_id": {"type": "string"},
                "kind": {"type": "string", "enum": ["preference", "fact", "event", "relationship", "commitment", "behavior"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["action"],
        },
        handler=_memory_tool,
        toolset="memory",
    )


def memory_buffer_tool_entry() -> ToolEntry:
    return ToolEntry(
        name="memory_buffer",
        description="Review and manage pending internal-memory observations.",
        schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "consolidate", "apply", "discard", "refresh_snapshot"]},
                "observation_id": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "applied", "skipped", "conflict"]},
            },
            "required": ["action"],
        },
        handler=_memory_buffer_tool,
        toolset="memory",
        is_parallel_safe=False,
    )


async def _memory_tool(
    action: str,
    content: str = "",
    query: str = "",
    memory_id: str = "",
    kind: str = "fact",
    limit: int = 10,
) -> str:
    manager, session_key = _runtime()
    if action == "add":
        return _json((await manager.add_external(content, kind=kind, session_key=session_key)).as_dict())
    if action == "search":
        return _json(await manager.search_entries(query, target="external", session_key=session_key))
    if action == "list":
        resolved_limit = max(1, min(int(limit or 10), 10))
        entries = await manager.list_entries(
            target="external",
            session_key=session_key,
            limit=resolved_limit + 1,
        )
        has_more = len(entries) > resolved_limit
        items = [_compact_memory_entry(item) for item in entries[:resolved_limit]]
        return _json({
            "items": items,
            "returned": len(items),
            "limit": resolved_limit,
            "has_more": has_more,
        })
    if action == "delete":
        return _json({"deleted": await manager.delete(memory_id, target="external", session_key=session_key)})
    if action == "history":
        return _json(await manager.history(memory_id))
    raise ValueError(f"Unknown memory action: {action}")


async def _memory_buffer_tool(action: str, observation_id: str = "", status: str = "pending") -> str:
    manager, session_key = _runtime()
    if action == "list":
        return _json(await manager.buffer_entries(status=status, session_key=session_key))
    if action == "consolidate":
        return _json(await manager.consolidate_internal(session_key=session_key))
    if action == "apply":
        return _json({"applied": await manager.apply_internal(observation_id, session_key=session_key)})
    if action == "discard":
        return _json({"discarded": await manager.discard_internal(observation_id, session_key=session_key)})
    if action == "refresh_snapshot":
        agent = current_tool_agent()
        if agent is None:
            return _json({"refreshed": False})
        from personal_agent.agent.agent import _pin_memory_snapshot

        _pin_memory_snapshot(agent)
        agent._cached_system_prompt = None
        return _json({"refreshed": True, "revision": agent._internal_memory_snapshot.revision})
    raise ValueError(f"Unknown memory buffer action: {action}")


def _runtime():
    if _manager is None:
        raise RuntimeError("Memory runtime is not initialized")
    agent = current_tool_agent()
    return _manager, str(getattr(agent, "_memory_session_key", "") or "")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _compact_memory_entry(entry: dict) -> dict:
    content = str(entry.get("content") or entry.get("text") or "")
    truncated = len(content) > 400
    if truncated:
        content = content[:397].rstrip() + "..."
    return {
        "id": str(entry.get("id") or ""),
        "content": content,
        "content_truncated": truncated,
        "kind": str(entry.get("kind") or ""),
        "importance": float(entry.get("importance") or 0.0),
        "provider": str(entry.get("source_provider") or entry.get("provider") or ""),
        "created_at": str(entry.get("created_at") or ""),
    }
