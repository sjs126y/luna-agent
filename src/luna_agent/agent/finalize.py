"""finalize_turn — persist new messages to DB, update session counters."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def unpack_message(msg: dict) -> tuple[str, str, list | None, str | None, str | None]:
    """Unpack an Anthropic-format message dict into flat fields for DB storage.

    Returns: (role, content, tool_calls, tool_name, tool_call_id)

    Tool messages are flattened: tool_use/tool_result blocks are NOT stored
    in the content column — only text goes there. This prevents orphan
    tool_use errors when loading history for subsequent turns.
    """
    role = msg.get("role", "user")
    content = ""
    tool_calls = None
    tool_name = None
    tool_call_id = None

    if isinstance(msg.get("content"), list):
        text_parts = []
        for block in msg["content"]:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls = tool_calls or []
                tool_calls.append({
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "input": block.get("input", {}),
                })
                tool_name = block.get("name")
            elif block.get("type") == "tool_result":
                # Extract text from tool_result, discard block metadata
                result = block.get("content", "")
                if isinstance(result, str):
                    text_parts.append(result)
                elif isinstance(result, list):
                    for r in result:
                        if isinstance(r, dict) and r.get("type") == "text":
                            text_parts.append(r.get("text", ""))
                tool_call_id = block.get("tool_use_id", "")
        content = " ".join(text_parts)
    elif isinstance(msg.get("content"), str):
        content = msg["content"]

    return role, content, tool_calls, tool_name, tool_call_id


async def finalize_turn(db, session_id: str, ctx, previous_message_count: int) -> None:
    """Persist new messages added during this turn."""
    new_messages = ctx.messages[previous_message_count:]
    if not new_messages:
        return

    for msg in new_messages:
        role, content, tool_calls, tool_name, tool_call_id = unpack_message(msg)
        await db.save_message(
            session_id=session_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )

    await db.update_last_active(session_id, increment_message=True)
    logger.debug("Persisted %d messages for session %s", len(new_messages), session_id)
