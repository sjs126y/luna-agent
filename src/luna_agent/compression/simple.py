"""Codex-style context compaction with handoff summaries."""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

from luna_agent.compression.base import CompactionMetadata, CompactionResult, ContextEngine
from luna_agent.compression.registry import compression_registry
from luna_agent.context_budget import resolve_compression_threshold
from luna_agent.llm.provider import ProviderProfile
from luna_agent.llm.token_counter import count_messages_tokens, estimate_tokens

logger = logging.getLogger(__name__)

SUMMARY_PREFIX = "[Context checkpoint summary]\n"
LEGACY_SUMMARY_PREFIX = "[系统生成的对话历史摘要]\n"
DEFAULT_RETAINED_USER_TOKENS = 20_000

HANDOFF_SYSTEM_PROMPT = """You are performing a CONTEXT CHECKPOINT COMPACTION.
Create a handoff summary for another LLM that will resume the task.

Include:
- Current progress and every key decision already made
- Important implementation details, constraints, corrections, and user preferences
- Rejected approaches that must not be repeated
- Files, commands, configuration, errors, tool outcomes, and references needed to continue
- What remains to be done, with enough detail to resume immediately

Completeness is more important than brevity. Avoid repetition, but do not omit confirmed
details merely to make the summary shorter. Do not impose a word or token limit on yourself.
"""


class ContextCompressor(ContextEngine):
    name = "compressor"

    def __init__(
        self,
        context_length: int = 256_000,
        threshold_ratio: float = 0.9,
        tail_token_budget: int = 20_000,
        retained_user_tokens: int = DEFAULT_RETAINED_USER_TOKENS,
        compressor_transport: Any = None,
        model: str = "",
        output_tokens: int = 4096,
    ) -> None:
        self.context_length = context_length
        self.threshold_tokens = resolve_compression_threshold(
            context_length,
            output_tokens,
            threshold_ratio,
        )
        self.tail_token_budget = tail_token_budget
        self.retained_user_tokens = retained_user_tokens
        self._compressor_transport = compressor_transport
        self.model = model
        # This is the selected provider's normal output allowance, not a
        # compaction-specific summary cap.
        self.output_tokens = output_tokens
        self.last_prompt_tokens = 0

    def should_compress(self, token_count: int, messages: list[dict]) -> bool:
        return token_count >= self.threshold_tokens and len(messages) > 1

    async def compress(
        self,
        messages: list[dict],
        system_prompt: str,
        transport: Any,
        *,
        trigger: str = "auto",
    ) -> CompactionResult:
        before_tokens = self._estimate_tokens(messages, system_prompt)
        self.last_prompt_tokens = before_tokens

        handoff_messages, reduced_tool_results = _fit_handoff_input(
            messages,
            token_budget=self.threshold_tokens,
            model=self.model,
        )
        summary = await self._generate_summary(handoff_messages, transport)
        if not summary:
            raise RuntimeError("compaction model returned an empty handoff summary")

        replacement, retained_user_tokens = self._build_replacement_history(messages, summary)
        after_tokens = self._estimate_tokens(replacement, system_prompt)
        summary_tokens = count_messages_tokens([_summary_message(summary)], model=self.model)
        metadata = CompactionMetadata(
            trigger=trigger,
            pre_tokens=before_tokens,
            post_tokens=after_tokens,
            summary_tokens=summary_tokens,
            retained_user_tokens=retained_user_tokens,
            pre_message_count=len(messages),
            post_message_count=len(replacement),
            model=self.model,
            details={"reduced_tool_results": reduced_tool_results},
        )
        logger.info(
            "Compacted context checkpoint: %d -> %d tokens (%d user tokens retained)",
            before_tokens,
            after_tokens,
            retained_user_tokens,
        )
        return CompactionResult(replacement, summary, metadata)

    def on_session_start(self) -> None:
        self.last_prompt_tokens = 0

    def on_session_end(self) -> None:
        self.last_prompt_tokens = 0

    async def _generate_summary(self, messages: list[dict], transport: Any) -> str | None:
        use_transport = self._compressor_transport or transport
        formatted = _format_messages_for_summary(messages)
        try:
            response = await use_transport.call(
                messages=[{
                    "role": "user",
                    "content": [{
                        "type": "text",
                        "text": "Create the checkpoint handoff from this conversation:\n\n" + formatted,
                    }],
                }],
                system_prompt=HANDOFF_SYSTEM_PROMPT,
                max_tokens=self.output_tokens,
            )
        except Exception:
            logger.exception("Compaction LLM call failed")
            return None
        summary = str(getattr(response, "text", "") or "").strip()
        return summary or None

    def _build_replacement_history(
        self,
        messages: list[dict],
        summary: str,
    ) -> tuple[list[dict], int]:
        tail_indices = _select_recent_indices(messages, self.tail_token_budget, self.model)
        tail_indices = _complete_tool_pairs(messages, tail_indices)

        user_indices, retained_user_tokens = _select_user_indices(
            messages,
            excluded=tail_indices,
            token_budget=self.retained_user_tokens,
            model=self.model,
        )
        selected = sorted(user_indices | tail_indices)
        replacement = [copy.deepcopy(messages[index]) for index in selected]
        replacement = [message for message in replacement if not is_summary_message(message)]
        replacement.append(_summary_message(summary))
        return replacement, retained_user_tokens

    def _estimate_tokens(self, messages: list[dict], system_prompt: str) -> int:
        return (
            count_messages_tokens(messages, model=self.model)
            + count_messages_tokens([], system_prompt, model=self.model)
        )


def is_summary_message(message: dict) -> bool:
    text = _message_text(message).lstrip()
    return text.startswith(SUMMARY_PREFIX.rstrip()) or text.startswith(
        LEGACY_SUMMARY_PREFIX.rstrip()
    )


def _summary_message(summary: str) -> dict:
    return {
        "role": "user",
        "content": [{"type": "text", "text": SUMMARY_PREFIX + summary.strip()}],
    }


def _select_recent_indices(messages: list[dict], token_budget: int, model: str) -> set[int]:
    if token_budget <= 0:
        return set()
    selected: set[int] = set()
    remaining = token_budget
    for index in range(len(messages) - 1, -1, -1):
        if is_summary_message(messages[index]):
            continue
        cost = _message_tokens(messages[index], model)
        if cost > remaining:
            break
        selected.add(index)
        remaining -= cost
    return selected


def _select_user_indices(
    messages: list[dict],
    *,
    excluded: set[int],
    token_budget: int,
    model: str,
) -> tuple[set[int], int]:
    selected: set[int] = set()
    used = 0
    for index in range(len(messages) - 1, -1, -1):
        if index in excluded or not _is_real_user_message(messages[index]):
            continue
        cost = _message_tokens(messages[index], model)
        if used + cost > token_budget:
            break
        selected.add(index)
        used += cost
    return selected, used


def _is_real_user_message(message: dict) -> bool:
    if str(message.get("role") or "") != "user" or is_summary_message(message):
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return bool(str(content or "").strip())
    has_text = False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return False
        if block.get("type") == "text" and str(block.get("text") or "").strip():
            has_text = True
    return has_text


def _complete_tool_pairs(messages: list[dict], selected: set[int]) -> set[int]:
    """Drop boundary tool blocks unless both call and result are retained."""
    calls: dict[str, int] = {}
    results: dict[str, int] = {}
    for index in selected:
        for block in _content_blocks(messages[index]):
            if block.get("type") == "tool_use" and block.get("id"):
                calls[str(block["id"])] = index
            elif block.get("type") == "tool_result" and block.get("tool_use_id"):
                results[str(block["tool_use_id"])] = index
    incomplete_indices = {
        index for tool_id, index in calls.items() if tool_id not in results
    } | {
        index for tool_id, index in results.items() if tool_id not in calls
    }
    return selected - incomplete_indices


def _content_blocks(message: dict) -> list[dict]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _message_tokens(message: dict, model: str) -> int:
    return max(1, count_messages_tokens([message], model=model))


def _message_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _format_messages_for_summary(messages: list[dict]) -> str:
    """Serialize full history for the handoff model without fixed truncation."""
    lines: list[str] = []
    for message in messages:
        role = str(message.get("role") or "unknown")
        parts: list[str] = []
        content = message.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    parts.append(str(block))
                    continue
                kind = str(block.get("type") or "")
                if kind == "text":
                    parts.append(str(block.get("text") or ""))
                elif kind == "tool_use":
                    parts.append(
                        "[tool call] "
                        + str(block.get("name") or "unknown")
                        + " "
                        + json.dumps(block.get("input") or {}, ensure_ascii=False, default=str)
                    )
                elif kind == "tool_result":
                    parts.append(
                        "[tool result] "
                        + str(block.get("tool_use_id") or "")
                        + " "
                        + _stringify_tool_result(block.get("content"))
                    )
                else:
                    parts.append(json.dumps(block, ensure_ascii=False, default=str))
        else:
            parts.append(str(content))
        lines.append(f"{role}: " + "\n".join(parts))
    return "\n\n".join(lines)


def _stringify_tool_result(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _fit_handoff_input(
    messages: list[dict],
    *,
    token_budget: int,
    model: str,
) -> tuple[list[dict], int]:
    """Reduce oldest oversized tool blobs only when compaction input cannot fit."""
    if token_budget <= 0 or count_messages_tokens(messages, model=model) <= token_budget:
        return messages, 0

    fitted = copy.deepcopy(messages)
    reduced = 0
    for message in fitted:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            current_tokens = count_messages_tokens(fitted, model=model)
            if current_tokens <= token_budget:
                return fitted, reduced
            raw = _stringify_tool_result(block.get("content"))
            raw_tokens = estimate_tokens(raw, model)
            if raw_tokens <= 0:
                continue
            excess = current_tokens - token_budget
            keep_tokens = max(0, raw_tokens - excess)
            block["content"] = _condense_tool_result(raw, keep_tokens, raw_tokens)
            reduced += 1
    return fitted, reduced


def _condense_tool_result(text: str, keep_tokens: int, original_tokens: int) -> str:
    if keep_tokens <= 0:
        return f"[tool result omitted during context recovery: {original_tokens} tokens]"
    keep_chars = min(len(text), max(0, int(len(text) * keep_tokens / max(original_tokens, 1))))
    if keep_chars >= len(text):
        return text
    head_chars = (keep_chars + 1) // 2
    tail_chars = keep_chars // 2
    omitted = len(text) - keep_chars
    tail = text[-tail_chars:] if tail_chars else ""
    return (
        text[:head_chars]
        + f"\n[... {omitted} characters omitted during context recovery ...]\n"
        + tail
    )


def build_simple_compressor(
    settings: Any,
    provider: ProviderProfile,
    api_mode: str,
) -> ContextCompressor | None:
    compressor_transport = None
    output_tokens = int(provider.max_tokens or 4096)
    if settings.compressor_model:
        comp_provider = ProviderProfile(
            name="compressor",
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.compressor_model,
            max_tokens=output_tokens,
        )
        from luna_agent.llm.transport_registry import transport_registry

        compressor_transport = transport_registry.get(api_mode, comp_provider)

    return ContextCompressor(
        context_length=provider.context_window or 256_000,
        threshold_ratio=settings.compression_threshold_ratio,
        tail_token_budget=settings.tail_token_budget,
        retained_user_tokens=int(getattr(settings, "retained_user_tokens", 20_000) or 20_000),
        compressor_transport=compressor_transport,
        model=provider.model or "",
        output_tokens=output_tokens,
    )


compression_registry.register("simple", build_simple_compressor, aliases=("compressor",))
