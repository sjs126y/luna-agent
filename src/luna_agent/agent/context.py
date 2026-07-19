"""build_turn_context — assemble messages, check tokens, apply compression."""

from __future__ import annotations

import copy
import inspect
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from luna_agent.context_budget import compose_context_text
from luna_agent.llm.token_counter import count_messages_tokens, count_tools_tokens
from luna_agent.text_safety import clean_text

if TYPE_CHECKING:
    from luna_agent.compression import CompactionResult
    from luna_agent.multimodal.processor import ResolvedConversationInput

logger = logging.getLogger(__name__)

CONTEXT_LIMIT = 256000   # fallback when provider metadata is unavailable
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
    compaction_result: "CompactionResult | None" = None
    skill_injection: str | None = None      # from /skill-name, injected to api_messages
    skill_summaries: str = ""               # ephemeral, injected to api_messages
    memory_prefetch_messages: list[dict] = field(default_factory=list)  # ephemeral
    memory_injections_text: str = ""        # for /usage diagnostics
    resolved_input: "ResolvedConversationInput | None" = None
    processed_attachments: list = field(default_factory=list)
    multimodal_diagnostics: dict = field(default_factory=dict)
    hook_contexts: list[str] = field(default_factory=list)  # ephemeral model context


async def build_turn_context(
    agent,
    user_message: str | "ResolvedConversationInput",
    history: list[dict] | None = None,
    *,
    turn_id: str | None = None,
) -> TurnContext:
    """Prepare messages for a conversation turn.
    Does NOT build api_messages — that happens inside the while loop.
    """
    import time
    import uuid

    from luna_agent.multimodal.processor import ResolvedConversationInput

    resolved_input = user_message if isinstance(user_message, ResolvedConversationInput) else None
    content_blocks = (
        list(resolved_input.content_blocks)
        if resolved_input is not None
        else [{"type": "text", "text": clean_text(str(user_message or ""))}]
    )
    text_message = clean_text(
        resolved_input.text if resolved_input is not None else str(user_message or "")
    )
    resolved_turn_id = str(turn_id or "").strip() or f"{uuid.uuid4().hex[:8]}"

    # Reset per-turn state
    agent._iteration_budget = agent.max_iterations
    agent._retry.reset()
    agent._interrupt_requested = False
    agent._tool_calls_this_turn = 0
    agent._destructive_calls_this_turn = 0
    agent._last_skill_injection = ""
    agent._last_skill_summaries = ""
    agent._last_memory_injections = ""
    agent._last_tool_results = []
    from luna_agent.artifacts import TurnResponseDraft
    agent._response_draft = TurnResponseDraft(
        session_key=str(getattr(agent, "_memory_session_key", "") or ""),
        turn_id=resolved_turn_id,
    )
    agent._hook_turn_id = resolved_turn_id
    agent._hook_additional_contexts = []
    from luna_agent.tools.executor import clear_interrupted
    clear_interrupted()

    # Refresh tools (if registry changed)
    from luna_agent.agent.agent import _refresh_tools, _build_system_prompt, _maybe_refresh_memory_snapshot
    _refresh_tools(agent)
    _maybe_refresh_memory_snapshot(agent)
    if agent._cached_system_prompt is None:
        _build_system_prompt(agent, agent._system_prompt_template)

    # Copy history
    conversation_history = list(history or [])
    messages = copy.deepcopy(conversation_history)

    current_user_message = {
        "role": "user",
        "content": content_blocks,
    }
    # Consume pending skill injection (set by Gateway /skill-name)
    skill_injection = None
    if agent._pending_skill_injection:
        skill_injection = agent._pending_skill_injection
        agent._pending_skill_injection = None  # consumed, won't leak to next turn

    skill_summaries = _load_skill_summaries()
    memory_prefetch_messages, memory_injections_text = await _prefetch_memory(agent, text_message)
    agent._last_skill_summaries = skill_summaries or ""
    agent._last_skill_injection = skill_injection or ""
    agent._last_memory_injections = memory_injections_text or ""

    # Token check + compression. Ephemeral injections count toward the request
    # budget but are not persisted into compressed history.
    candidate_messages = messages + [current_user_message]
    pre_count = len(candidate_messages)
    compaction_result = await _check_and_build_compaction(
        agent,
        candidate_messages,
        messages,
        extra_context_text=compose_context_text(
            skill_summaries,
            skill_injection or "",
            memory_injections_text,
        ),
    )
    compression_hook_contexts = list(agent._hook_additional_contexts)
    agent._hook_additional_contexts.clear()
    if compaction_result is not None:
        messages = copy.deepcopy(compaction_result.replacement_history)
    messages.append(current_user_message)
    was_compressed = compaction_result is not None
    user_idx = max(0, len(messages) - 1)

    return TurnContext(
        user_message=text_message,
        original_user_message=text_message,
        messages=messages,
        conversation_history=conversation_history,
        active_system_prompt=agent._cached_system_prompt or "",
        turn_id=resolved_turn_id,
        current_turn_user_idx=user_idx,
        was_compressed=was_compressed,
        pre_compress_message_count=pre_count,
        compaction_result=compaction_result,
        skill_injection=skill_injection,
        skill_summaries=skill_summaries,
        memory_prefetch_messages=memory_prefetch_messages,
        memory_injections_text=memory_injections_text,
        should_review_memory=False,
        resolved_input=resolved_input,
        processed_attachments=list(resolved_input.attachments) if resolved_input is not None else [],
        multimodal_diagnostics=dict(resolved_input.diagnostics) if resolved_input is not None else {},
        hook_contexts=compression_hook_contexts,
    )


async def _check_and_compress(
    agent,
    messages: list[dict],
    *,
    extra_context_text: str = "",
) -> list[dict]:
    """Compatibility wrapper returning only replacement history."""
    compaction = await _check_and_build_compaction(
        agent,
        messages,
        messages,
        extra_context_text=extra_context_text,
    )
    return compaction.replacement_history if compaction is not None else messages


async def _check_and_build_compaction(
    agent,
    measured_messages: list[dict],
    completed_messages: list[dict],
    *,
    extra_context_text: str = "",
    trigger: str = "auto",
    force: bool = False,
):
    """Build a checkpoint for completed history when the candidate request is full."""
    if agent._compressor is None:
        return None

    model = agent._provider.model if agent._provider else ""
    total = (
        count_messages_tokens(measured_messages, model=model)
        + count_messages_tokens([], agent._cached_system_prompt or "", model=model)
        + count_tools_tokens(agent.tools, model=model)
        + count_messages_tokens([{
            "role": "user",
            "content": [{"type": "text", "text": extra_context_text}],
        }], model=model)
    )

    if not force and not agent._compressor.should_compress(total, completed_messages):
        return None
    if force and len(completed_messages) <= 1:
        return None

    hook_manager = getattr(agent, "_hook_manager", None)
    if hook_manager is not None:
        from pathlib import Path

        from luna_agent.hooks import HookEnvelope, HookEvent, HookScope, HookSourceContext

        source = getattr(agent, "_hook_source", None)
        security_context = getattr(agent, "_security_context", None)
        common = {
            "scope": HookScope.TURN,
            "session_key": str(
                getattr(security_context, "session_key", "")
                or getattr(agent, "_memory_session_key", "")
            ),
            "turn_id": str(getattr(agent, "_hook_turn_id", "") or ""),
            "cwd": str(Path.cwd()),
            "mode": str(getattr(security_context, "mode_id", "") or ""),
            "source": HookSourceContext(
                platform=str(getattr(source, "platform", "") or ""),
                user_id=str(getattr(source, "user_id", "") or ""),
                chat_id=str(getattr(source, "chat_id", "") or ""),
            ),
        }
        pre_outcome = await hook_manager.dispatch(HookEnvelope(
            event_name=HookEvent.PRE_COMPACT,
            payload={
                "trigger": trigger,
                "message_count": len(completed_messages),
                "estimated_tokens": total,
            },
            **common,
        ))
        if pre_outcome.additional_context.strip():
            agent._hook_additional_contexts.append(
                f"[PreCompact hook context]\n{pre_outcome.additional_context.strip()}"
            )
        if pre_outcome.stop:
            return None

    logger.info("Compressing: %d tokens > %d limit", total, agent._compressor.threshold_tokens)
    try:
        compress_kwargs = {}
        try:
            params = inspect.signature(agent._compressor.compress).parameters
            if "trigger" in params or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in params.values()
            ):
                compress_kwargs["trigger"] = trigger
        except (TypeError, ValueError):
            compress_kwargs["trigger"] = trigger
        compaction = await agent._compressor.compress(
            completed_messages,
            agent._cached_system_prompt or "",
            agent._transport,
            **compress_kwargs,
        )
        # Third-party engines written against the original contract may still
        # return the replacement list directly. Keep that shape readable while
        # built-ins expose checkpoint metadata through CompactionResult.
        if not hasattr(compaction, "replacement_history"):
            from luna_agent.compression import CompactionMetadata, CompactionResult

            replacement = list(compaction or [])
            compaction = CompactionResult(
                replacement_history=replacement,
                summary="",
                metadata=CompactionMetadata(
                    pre_message_count=len(completed_messages),
                    post_message_count=len(replacement),
                    model=model,
                    details={"legacy_engine": True},
                ),
            )
        result = compaction.replacement_history
        if hook_manager is not None:
            await hook_manager.dispatch(HookEnvelope(
                event_name=HookEvent.POST_COMPACT,
                payload={
                    "trigger": trigger,
                    "pre_message_count": len(completed_messages),
                    "post_message_count": len(result),
                    "fallback": False,
                },
                **common,
            ))
        return compaction
    except Exception:
        logger.exception("Compression failed; preserving original context")
        result = completed_messages
        if hook_manager is not None:
            await hook_manager.dispatch(HookEnvelope(
                event_name=HookEvent.POST_COMPACT,
                payload={
                    "trigger": trigger,
                    "pre_message_count": len(completed_messages),
                    "post_message_count": len(result),
                    "fallback": True,
                },
                **common,
            ))
        return None


def _truncate(messages: list[dict], head: int = 2, tail: int = 6) -> list[dict]:
    """Fallback: drop oldest messages except protected ones."""
    if len(messages) <= head + tail:
        return messages
    return messages[:head] + messages[-tail:]


def _load_skill_summaries() -> str:
    try:
        from luna_agent.skills.registry import skill_registry

        return skill_registry.get_summaries() or ""
    except Exception:
        return ""


async def _prefetch_memory(agent, user_message: str) -> tuple[list[dict], str]:
    memory_manager = getattr(agent, "_memory_manager", None)
    if memory_manager is None:
        return [], ""
    try:
        from luna_agent.context_budget import message_text

        try:
            prefetched = await memory_manager.prefetch(
                user_message, session_key=getattr(agent, "_memory_session_key", "")
            )
        except TypeError as exc:
            if "session_key" not in str(exc):
                raise
            prefetched = await memory_manager.prefetch(user_message)
        messages = [item for item in prefetched if item]
        text = "\n".join(message_text(item) for item in messages)
        return messages, text
    except Exception:
        return [], ""
