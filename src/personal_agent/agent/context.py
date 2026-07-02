"""build_turn_context — assemble messages, check tokens, apply compression."""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field

from personal_agent.context_budget import compose_context_text
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
    was_compressed: bool = False            # True if compression ran this turn
    pre_compress_message_count: int = 0     # message count before compression
    skill_injection: str | None = None      # from /skill-name, injected to api_messages
    skill_summaries: str = ""               # ephemeral, injected to api_messages
    memory_prefetch_messages: list[dict] = field(default_factory=list)  # ephemeral
    memory_injections_text: str = ""        # for /usage diagnostics


async def build_turn_context(
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
    agent._tool_calls_this_turn = 0
    agent._destructive_calls_this_turn = 0
    agent._destructive_allowed.clear()
    agent._last_skill_injection = ""
    agent._last_skill_summaries = ""
    agent._last_memory_injections = ""
    agent._last_tool_results = []
    from personal_agent.tools.executor import clear_interrupted
    clear_interrupted()

    # Refresh tools (if registry changed)
    from personal_agent.agent.agent import _refresh_tools, _build_system_prompt
    _refresh_tools(agent)
    if agent._cached_system_prompt is None:
        _build_system_prompt(agent, agent._system_prompt_template)

    # Memory review nudge — every N turns
    should_review = False
    if agent._memory_review_interval > 0:
        agent._turns_since_memory += 1
        if agent._turns_since_memory >= agent._memory_review_interval:
            should_review = True
            agent._turns_since_memory = 0

    # Copy history
    conversation_history = list(history or [])
    messages = copy.deepcopy(conversation_history)

    # Append current user message
    messages.append({
        "role": "user",
        "content": [{"type": "text", "text": user_message}],
    })
    # Consume pending skill injection (set by Gateway /skill-name)
    skill_injection = None
    if agent._pending_skill_injection:
        skill_injection = agent._pending_skill_injection
        agent._pending_skill_injection = None  # consumed, won't leak to next turn

    skill_summaries = _load_skill_summaries()
    memory_prefetch_messages, memory_injections_text = await _prefetch_memory(agent, user_message)
    agent._last_skill_summaries = skill_summaries or ""
    agent._last_skill_injection = skill_injection or ""
    agent._last_memory_injections = memory_injections_text or ""

    # Token check + compression. Ephemeral injections count toward the request
    # budget but are not persisted into compressed history.
    pre_count = len(messages)
    pre_compress_messages = copy.deepcopy(messages)
    messages = await _check_and_compress(
        agent,
        messages,
        extra_context_text=compose_context_text(
            skill_summaries,
            skill_injection or "",
            memory_injections_text,
        ),
    )
    was_compressed = messages != pre_compress_messages
    user_idx = max(0, len(messages) - 1)

    turn_id = f"{uuid.uuid4().hex[:8]}"

    return TurnContext(
        user_message=user_message,
        original_user_message=user_message,
        messages=messages,
        conversation_history=conversation_history,
        active_system_prompt=agent._cached_system_prompt or "",
        turn_id=turn_id,
        current_turn_user_idx=user_idx,
        was_compressed=was_compressed,
        pre_compress_message_count=pre_count,
        skill_injection=skill_injection,
        skill_summaries=skill_summaries,
        memory_prefetch_messages=memory_prefetch_messages,
        memory_injections_text=memory_injections_text,
        should_review_memory=should_review,
    )


async def _check_and_compress(
    agent,
    messages: list[dict],
    *,
    extra_context_text: str = "",
) -> list[dict]:
    """If estimated tokens exceed threshold, compress via ContextEngine."""
    if agent._compressor is None:
        return messages

    model = agent._provider.model if agent._provider else ""
    total = (
        count_messages_tokens(messages, model=model)
        + count_messages_tokens([], agent._cached_system_prompt or "", model=model)
        + count_tools_tokens(agent.tools, model=model)
        + count_messages_tokens([{
            "role": "user",
            "content": [{"type": "text", "text": extra_context_text}],
        }], model=model)
    )

    if not agent._compressor.should_compress(total, messages):
        return messages

    logger.info("Compressing: %d tokens > %d limit", total, agent._compressor.threshold_tokens)
    try:
        result = await agent._compressor.compress(
            messages,
            agent._cached_system_prompt or "",
            agent._transport,
        )
        return result
    except Exception:
        logger.exception("Compression failed, falling back to truncation")
        return _truncate(messages, agent._compressor.protect_head, agent._compressor.protect_tail)


def _truncate(messages: list[dict], head: int = 2, tail: int = 6) -> list[dict]:
    """Fallback: drop oldest messages except protected ones."""
    if len(messages) <= head + tail:
        return messages
    return messages[:head] + messages[-tail:]


def _load_skill_summaries() -> str:
    try:
        from personal_agent.skills.registry import skill_registry

        return skill_registry.get_summaries() or ""
    except Exception:
        return ""


async def _prefetch_memory(agent, user_message: str) -> tuple[list[dict], str]:
    memory_manager = getattr(agent, "_memory_manager", None)
    if memory_manager is None:
        return [], ""
    try:
        from personal_agent.context_budget import message_text

        prefetched = await memory_manager.prefetch(user_message)
        messages = [item for item in prefetched if item]
        text = "\n".join(message_text(item) for item in messages)
        return messages, text
    except Exception:
        return [], ""
