"""build_turn_context — assemble messages, check tokens, apply compression."""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field

from personal_agent.llm.token_counter import count_messages_tokens, count_tools_tokens

logger = logging.getLogger(__name__)

CONTEXT_LIMIT = 64000    # DeepSeek context window
THRESHOLD = 0.6          # compress at 60% usage
PROTECT_FIRST = 2        # messages at head to protect
PROTECT_LAST = 6         # messages at tail to protect


@dataclass
class TurnContext:
    user_message: str
    original_user_message: str
    messages: list[dict]              # working copy, persisted
    conversation_history: list[dict]  # read-only original from DB
    active_system_prompt: str
    turn_id: str = ""
    current_turn_user_idx: int = 0
    should_review_memory: bool = False


def build_turn_context(
    agent,
    user_message: str,
    history: list[dict] | None = None,
) -> TurnContext:
    """Prepare messages for a conversation turn.
    Does NOT build api_messages — that happens inside the while loop.
    """
    import time
    import uuid

    # Reset per-turn state
    agent._iteration_budget = agent.max_iterations
    agent._retry.reset()
    agent._interrupt_requested = False

    # Refresh tools (if registry changed)
    from personal_agent.agent.agent import _refresh_tools, _build_system_prompt
    _refresh_tools(agent)
    if agent._cached_system_prompt is None:
        _build_system_prompt(agent)

    # Copy history
    conversation_history = list(history or [])
    messages = copy.deepcopy(conversation_history)

    # Append current user message
    messages.append({
        "role": "user",
        "content": [{"type": "text", "text": user_message}],
    })
    user_idx = len(messages) - 1

    # Token check + compression
    messages = _check_and_compress(agent, messages)

    turn_id = f"{uuid.uuid4().hex[:8]}"

    return TurnContext(
        user_message=user_message,
        original_user_message=user_message,
        messages=messages,
        conversation_history=conversation_history,
        active_system_prompt=agent._cached_system_prompt or "",
        turn_id=turn_id,
        current_turn_user_idx=user_idx,
    )


def _check_and_compress(agent, messages: list[dict]) -> list[dict]:
    """If estimated tokens exceed threshold, compress old messages."""
    if agent._compressor is None:
        return messages

    total = (
        count_messages_tokens(messages)
        + count_messages_tokens([], agent._cached_system_prompt or "")
        + count_tools_tokens(agent.tools)
    )
    if total < CONTEXT_LIMIT * THRESHOLD:
        return messages

    if len(messages) <= PROTECT_FIRST + PROTECT_LAST + 2:
        return messages  # too few to compress

    logger.info("Compressing: %d tokens > %d limit", total, int(CONTEXT_LIMIT * THRESHOLD))
    try:
        return agent._compressor.compress(
            messages, agent._cached_system_prompt or "",
            agent._transport,
            protect_head=PROTECT_FIRST, protect_tail=PROTECT_LAST,
        )
    except Exception:
        logger.exception("Compression failed, falling back to truncation")
        return _truncate(messages)


def _truncate(messages: list[dict]) -> list[dict]:
    """Fallback: drop oldest messages except protected ones."""
    if len(messages) <= PROTECT_FIRST + PROTECT_LAST:
        return messages
    return (
        messages[:PROTECT_FIRST]
        + messages[-(PROTECT_LAST):]
    )
