"""Build safe conversation transcripts for interrupted turns."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PartialTranscript:
    messages: list[dict]
    summary: dict[str, Any]


def build_stopped_turn_transcript(
    messages: list[dict],
    *,
    current_turn_user_idx: int | None,
    user_text: str,
    stop_text: str,
) -> PartialTranscript:
    """Keep the durable prefix of a stopped turn without orphaned tool blocks."""
    turn_messages = _current_turn_messages(
        messages,
        current_turn_user_idx=current_turn_user_idx,
        user_text=user_text,
    )
    result_ids = _tool_result_ids(turn_messages)
    retained_tool_ids: set[str] = set()
    saved: list[dict] = []
    dropped = 0

    for message in turn_messages:
        content = message.get("content")
        if not isinstance(content, list):
            saved.append(copy.deepcopy(message))
            continue

        blocks: list[dict] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_use":
                tool_id = str(block.get("id") or "")
                if tool_id and tool_id in result_ids:
                    blocks.append(copy.deepcopy(block))
                    retained_tool_ids.add(tool_id)
                else:
                    dropped += 1
            elif block_type == "tool_result":
                tool_id = str(block.get("tool_use_id") or "")
                if tool_id and tool_id in retained_tool_ids:
                    blocks.append(copy.deepcopy(block))
            else:
                blocks.append(copy.deepcopy(block))

        if blocks:
            retained = copy.deepcopy(message)
            retained["content"] = blocks
            saved.append(retained)

    marker = str(stop_text or "").strip() or "已停止。"
    if not _ends_with_assistant_text(saved, marker):
        saved.append({
            "role": "assistant",
            "content": [{"type": "text", "text": marker}],
        })

    return PartialTranscript(
        messages=saved,
        summary={
            "partial": True,
            "messages_saved": len(saved),
            "tool_calls_saved": len(retained_tool_ids),
            "incomplete_tool_calls_dropped": dropped,
        },
    )


def _current_turn_messages(
    messages: list[dict],
    *,
    current_turn_user_idx: int | None,
    user_text: str,
) -> list[dict]:
    if current_turn_user_idx is not None and 0 <= current_turn_user_idx < len(messages):
        return copy.deepcopy(messages[current_turn_user_idx:])

    expected = str(user_text or "").strip()
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "user" or _has_tool_results(message):
            continue
        if not expected or _message_text(message).strip() == expected:
            return copy.deepcopy(messages[index:])

    return [{"role": "user", "content": [{"type": "text", "text": user_text}]}]


def _tool_result_ids(messages: list[dict]) -> set[str]:
    return {
        str(block.get("tool_use_id"))
        for message in messages
        for block in _content_blocks(message)
        if block.get("type") == "tool_result" and block.get("tool_use_id")
    }


def _content_blocks(message: dict) -> list[dict]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _has_tool_results(message: dict) -> bool:
    return any(block.get("type") == "tool_result" for block in _content_blocks(message))


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(
        str(block.get("text") or "")
        for block in _content_blocks(message)
        if block.get("type") == "text"
    )


def _ends_with_assistant_text(messages: list[dict], text: str) -> bool:
    if not messages or messages[-1].get("role") != "assistant":
        return False
    return _message_text(messages[-1]).strip() == text
