"""Anthropic Messages API transport — stream → NormalizedResponse."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from personal_agent.llm.base import BaseTransport, DeltaCallback, LLMRequestPlan
from personal_agent.llm.client import call_anthropic
from personal_agent.llm.provider import ProviderProfile
from personal_agent.models.messages import NormalizedResponse

logger = logging.getLogger(__name__)


class AnthropicMessagesTransport(BaseTransport):
    """Implements Anthropic Messages API wire format.
    Also compatible with DeepSeek's /anthropic endpoint.
    """

    def __init__(self, provider: ProviderProfile) -> None:
        self._provider = provider

    # ── build_request ──────────────────────────────────

    def build_request(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int,
    ) -> dict:
        converted = self.convert_messages(messages)

        body: dict = {
            "model": self._provider.model,
            "max_tokens": max_tokens or self._provider.max_tokens,
            "messages": converted,
        }
        if system_prompt:
            # Wrap system prompt as content block list with cache_control on last block
            body["system"] = [
                {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
            ]
        if tools:
            body["tools"] = self.convert_tool_definitions(_sorted_tools(tools))

        if self._provider.request_hook:
            body = self._provider.request_hook(body)
        return body

    # ── parse_stream ───────────────────────────────────

    async def parse_stream(
        self,
        stream: AsyncIterator[dict],
        on_delta: DeltaCallback | None = None,
    ) -> NormalizedResponse:
        """Parse stream events (SSE or single non-streaming JSON) → NormalizedResponse.

        If ``on_delta`` is provided, it is awaited with ("text", chunk) or
        ("thinking", chunk) as incremental content arrives. Omitting it keeps
        the original accumulate-then-return behavior (used by platform paths).
        """
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_use_blocks: dict[int, dict] = {}
        raw_usage: dict = {}
        stop_reason = ""
        model = self._provider.model
        seen_message = False  # tracks if we got the full message event (non-streaming)

        async def _emit(kind: str, chunk: str) -> None:
            if on_delta is not None and chunk:
                await on_delta(kind, chunk)

        async for event in stream:
            etype = event.get("type", "")

            # Non-streaming: DeepSeek returns a single "message" event
            if etype == "message":
                seen_message = True
                model = event.get("model", model)
                stop_reason = event.get("stop_reason", "")
                raw_usage.update(event.get("usage", {}) or {})

                for block in event.get("content", []):
                    btype = block.get("type")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                        await _emit("text", block.get("text", ""))
                    elif btype == "thinking":
                        thinking_parts.append(block.get("thinking", ""))
                        await _emit("thinking", block.get("thinking", ""))
                    elif btype == "tool_use":
                        tool_use_blocks[len(tool_use_blocks)] = {
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "input": block.get("input", {}),
                        }
                break  # single event, done

            # Streaming SSE events
            if etype == "message_start":
                msg = event.get("message", {})
                raw_usage.update(msg.get("usage", {}) or {})
                model = msg.get("model", model)

            elif etype == "content_block_start":
                block = event.get("content_block", {})
                idx = event.get("index", 0)
                if block.get("type") == "tool_use":
                    tool_use_blocks[idx] = {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "input_json": "",
                    }

            elif etype == "content_block_delta":
                delta = event.get("delta", {})
                idx = event.get("index", 0)
                dtype = delta.get("type")
                if dtype == "text_delta":
                    chunk = delta.get("text", "")
                    text_parts.append(chunk)
                    await _emit("text", chunk)
                elif dtype == "thinking_delta":
                    chunk = delta.get("thinking", "")
                    thinking_parts.append(chunk)
                    await _emit("thinking", chunk)
                elif dtype == "input_json_delta":
                    if idx in tool_use_blocks:
                        tool_use_blocks[idx]["input_json"] += delta.get("partial_json", "")

            elif etype == "message_delta":
                raw_usage.update(event.get("usage", {}) or {})
                stop_reason = event.get("delta", {}).get("stop_reason", "") or stop_reason

            elif etype == "message_stop":
                pass

        # Reassemble tool calls
        tool_calls = []
        for idx in sorted(tool_use_blocks.keys()):
            block = tool_use_blocks[idx]
            if "input" in block:
                inp = block["input"]  # non-streaming: already parsed
            else:
                try:
                    inp = json.loads(block.get("input_json", "")) if block.get("input_json") else {}
                except json.JSONDecodeError:
                    logger.warning("Failed to parse tool input JSON for %s", block.get("name"))
                    inp = {}
            tool_calls.append({
                "id": block["id"],
                "name": block["name"],
                "input": inp,
            })

        finish_reason = _map_stop_reason(stop_reason, bool(tool_calls))

        normalized = NormalizedResponse(
            text="".join(text_parts),
            thinking="".join(thinking_parts),
            tool_calls=tool_calls,
            usage=self.normalize_usage(raw_usage),
            finish_reason=finish_reason,
            stop_reason=stop_reason,
            model=model,
        )

        if self._provider.response_hook:
            normalized = self._provider.response_hook(normalized)

        return normalized

    # ── format conversions ─────────────────────────────

    def convert_tool_definitions(self, tools: list[dict]) -> list[dict]:
        """Internal tool dicts → Anthropic tool schema."""
        result = []
        for tool in tools:
            entry = {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": tool.get("input_schema", tool.get("parameters", {})),
            }
            result.append(entry)
        return result

    def convert_messages(self, messages: list[dict]) -> list[dict]:
        """Already in Anthropic format — pass through (fast path)."""
        return messages

    # ── convenience call ───────────────────────────────

    async def call(
        self,
        messages: list[dict],
        system_prompt: str = "",
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
        stream: bool = True,
        on_delta: DeltaCallback | None = None,
        request_plan: LLMRequestPlan | None = None,
    ) -> NormalizedResponse:
        """Build request, stream, parse — all in one call.

        The DeepSeek /anthropic endpoint supports SSE streaming (verified),
        including ``thinking`` deltas, so streaming is on by default. Pass
        ``on_delta`` to receive incremental text/thinking as it arrives.
        """
        if request_plan is not None:
            body = self.build_request_from_plan(request_plan, max_tokens)
        else:
            body = self.build_request(
                messages, system_prompt, tools or [], max_tokens
            )
        self.remember_cache_diagnostics(body, request_plan=request_plan)
        event_stream = call_anthropic(
            base_url=self._provider.base_url,
            api_key=self._provider.api_key,
            body=body,
            stream=stream,
            extra_headers=self._provider.extra_headers,
        )
        return await self.parse_stream(event_stream, on_delta=on_delta)


def _map_stop_reason(stop_reason: str, has_tool_calls: bool) -> str:
    """Map Anthropic stop_reason to our finish_reason."""
    if stop_reason == "end_turn":
        return "end_turn"
    if stop_reason == "tool_use":
        return "tool_use"
    if stop_reason == "max_tokens":
        return "max_tokens"
    if stop_reason == "stop_sequence":
        return "stop"
    # Fallback
    if has_tool_calls:
        return "tool_use"
    return "end_turn"


def _sorted_tools(tools: list[dict]) -> list[dict]:
    return sorted(tools, key=lambda item: str(item.get("name") or ""))
