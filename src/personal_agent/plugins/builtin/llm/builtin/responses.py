"""OpenAI Responses API transport."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from personal_agent.llm.base import BaseTransport, DeltaCallback, LLMRequestPlan
from personal_agent.llm.client import call_openai_responses
from personal_agent.llm.provider import ProviderProfile
from personal_agent.models.messages import NormalizedResponse

logger = logging.getLogger(__name__)


class OpenAIResponsesTransport(BaseTransport):
    """Implements the OpenAI Responses wire format.

    This is intentionally conservative: it supports text, image_url blocks,
    and function tools using the current Responses API shape. That is enough
    for the multimodal image-text fallback and keeps the main agent path
    compatible if a provider explicitly selects this wire API later.
    """

    def __init__(self, provider: ProviderProfile) -> None:
        self._provider = provider

    def build_request(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int,
    ) -> dict:
        body: dict = {
            "model": self._provider.model,
            "max_output_tokens": max_tokens or self._provider.max_tokens,
            "input": self.convert_messages(messages, system_prompt),
        }
        if tools:
            body["tools"] = self.convert_tool_definitions(_sorted_tools(tools))

        if self._provider.request_hook:
            body = self._provider.request_hook(body)
        return body

    async def parse_stream(
        self,
        stream: AsyncIterator[dict],
        on_delta: DeltaCallback | None = None,
    ) -> NormalizedResponse:
        text_parts: list[str] = []
        tool_call_deltas: dict[int, dict] = {}
        raw_usage: dict = {}
        finish_reason = ""
        model = self._provider.model

        async for event in stream:
            etype = str(event.get("type") or "")

            if "usage" in event:
                raw_usage.update(event.get("usage") or {})
            if event.get("model"):
                model = str(event.get("model") or model)

            if event.get("output_text"):
                chunk = str(event.get("output_text") or "")
                text_parts.append(chunk)
                if on_delta is not None:
                    await on_delta("text", chunk)
                finish_reason = str(event.get("status") or event.get("finish_reason") or finish_reason)
                continue

            if etype == "response.output_text.delta":
                chunk = str(event.get("delta") or "")
                text_parts.append(chunk)
                if on_delta is not None:
                    await on_delta("text", chunk)
                continue

            if etype == "response.completed":
                response = event.get("response") or {}
                if isinstance(response, dict):
                    raw_usage.update(response.get("usage") or {})
                    model = str(response.get("model") or model)
                    finish_reason = str(response.get("status") or finish_reason)
                    chunk = _collect_response_output(response, tool_call_deltas)
                    if chunk and not text_parts:
                        text_parts.append(chunk)
                continue

            if etype == "response.failed":
                error = event.get("error") or event.get("response", {}).get("error") or {}
                raise RuntimeError(f"Responses API failed: {error}")

            if event.get("output"):
                chunk = _collect_response_output(event, tool_call_deltas)
                if chunk and not text_parts:
                    text_parts.append(chunk)
                finish_reason = str(event.get("status") or finish_reason)
                continue

            _collect_tool_call_delta(event, tool_call_deltas)

        tool_calls = _build_tool_calls(tool_call_deltas)
        normalized = NormalizedResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            usage=self.normalize_usage(raw_usage),
            finish_reason="tool_calls" if tool_calls else (finish_reason or "stop"),
            stop_reason=finish_reason,
            model=model,
        )

        if self._provider.response_hook:
            normalized = self._provider.response_hook(normalized)

        return normalized

    def convert_tool_definitions(self, tools: list[dict]) -> list[dict]:
        result = []
        for tool in tools:
            result.append({
                "type": "function",
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", tool.get("parameters", {})),
            })
        return result

    def convert_messages(self, messages: list[dict], system_prompt: str = "") -> list[dict]:
        return self._convert_messages(messages, system_prompt, structured_tools=True)

    def _convert_messages(
        self,
        messages: list[dict],
        system_prompt: str = "",
        *,
        structured_tools: bool,
    ) -> list[dict]:
        result: list[dict] = []
        tool_names_by_id: dict[str, str] = {}

        if system_prompt:
            result.append({
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            })

        for msg in messages:
            role = str(msg.get("role") or "user")
            if role not in {"system", "developer", "user", "assistant"}:
                role = "user"
            content = msg.get("content", "")

            if isinstance(content, str):
                result.append({
                    "role": role,
                    "content": [{"type": _text_part_type(role), "text": content}],
                })
                continue

            if not isinstance(content, list):
                result.append({
                    "role": role,
                    "content": [{"type": _text_part_type(role), "text": str(content)}],
                })
                continue

            parts: list[dict] = []

            def flush_parts() -> None:
                if parts:
                    result.append({"role": role, "content": list(parts)})
                    parts.clear()

            for block in content:
                btype = block.get("type", "")
                if btype == "text":
                    parts.append({"type": _text_part_type(role), "text": str(block.get("text") or "")})
                elif btype == "image_url":
                    url = str((block.get("image_url") or {}).get("url") or "")
                    if url:
                        parts.append({"type": "input_image", "image_url": url})
                elif btype == "tool_result":
                    call_id = str(block.get("tool_use_id") or "")
                    if call_id and structured_tools:
                        flush_parts()
                        result.append({
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": str(block.get("content") or ""),
                        })
                    else:
                        parts.append({
                            "type": "input_text",
                            "text": _tool_result_text(block, tool_names_by_id),
                        })
                elif btype == "tool_use":
                    call_id = str(block.get("id") or "")
                    name = str(block.get("name") or "")
                    if call_id:
                        tool_names_by_id[call_id] = name
                    if structured_tools:
                        flush_parts()
                        result.append({
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        })
                    else:
                        parts.append({
                            "type": _text_part_type(role),
                            "text": _tool_call_text(block),
                        })

            flush_parts()

        return result

    async def call(
        self,
        messages: list[dict],
        system_prompt: str = "",
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
        stream: bool = False,
        on_delta: DeltaCallback | None = None,
        request_plan: LLMRequestPlan | None = None,
    ) -> NormalizedResponse:
        if request_plan is not None:
            body = self.build_request_from_plan(request_plan, max_tokens)
        else:
            body = self.build_request(messages, system_prompt, tools or [], max_tokens)
        self.remember_cache_diagnostics(body, request_plan=request_plan)
        event_stream = call_openai_responses(
            base_url=self._provider.base_url,
            api_key=self._provider.api_key,
            body=body,
            stream=stream or on_delta is not None,
            extra_headers=self._provider.extra_headers,
        )
        return await self.parse_stream(event_stream, on_delta=on_delta)


class CodexResponsesTransport(OpenAIResponsesTransport):
    """Semantic alias for Codex-style middle stations using Responses API."""

    def convert_messages(self, messages: list[dict], system_prompt: str = "") -> list[dict]:
        # Some Codex-style middle stations expose the Responses endpoint but do
        # not accept previous function_call/function_call_output items as input.
        # Keep the tool result visible to the model without using those item
        # types, so the main loop remains usable with those providers.
        return self._convert_messages(messages, system_prompt, structured_tools=False)


def _text_part_type(role: str) -> str:
    return "output_text" if role == "assistant" else "input_text"


def _responses_output_text(response: dict) -> str:
    return _collect_response_output(response, {})


def _tool_call_text(block: dict) -> str:
    name = str(block.get("name") or "tool")
    call_id = str(block.get("id") or "")
    arguments = json.dumps(block.get("input") or {}, ensure_ascii=False)
    suffix = f" call_id={call_id}" if call_id else ""
    return f"[Tool call requested: {name}{suffix} arguments={arguments}]"


def _tool_result_text(block: dict, tool_names_by_id: dict[str, str]) -> str:
    call_id = str(block.get("tool_use_id") or "")
    name = tool_names_by_id.get(call_id, "tool")
    content = str(block.get("content") or "")
    prefix = f"[Tool result for {name}"
    if call_id:
        prefix += f" call_id={call_id}"
    prefix += "]\n"
    return prefix + content


def _collect_response_output(response: dict, tool_call_deltas: dict[int, dict]) -> str:
    parts: list[str] = []
    for idx, item in enumerate(response.get("output") or []):
        if not isinstance(item, dict):
            continue
        itype = str(item.get("type") or "")
        if itype == "function_call":
            _collect_full_function_call(idx, item, tool_call_deltas)
            continue
        if itype in {"message", "output_message"}:
            for block in item.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in {"output_text", "text"}:
                    parts.append(str(block.get("text") or ""))
    return "".join(parts)


def _collect_full_function_call(idx: int, item: dict, tool_call_deltas: dict[int, dict]) -> None:
    entry = tool_call_deltas.setdefault(idx, {"id": "", "name": "", "arguments_json": ""})
    entry["id"] = str(item.get("call_id") or item.get("id") or entry["id"])
    entry["name"] = str(item.get("name") or entry["name"])
    arguments = item.get("arguments")
    if isinstance(arguments, dict):
        entry["arguments_json"] = json.dumps(arguments, ensure_ascii=False)
    elif arguments is not None:
        entry["arguments_json"] = str(arguments)


def _collect_tool_call_delta(event: dict, tool_call_deltas: dict[int, dict]) -> None:
    etype = str(event.get("type") or "")
    if etype == "response.function_call_arguments.delta":
        idx = int(event.get("output_index") or 0)
        entry = tool_call_deltas.setdefault(idx, {"id": "", "name": "", "arguments_json": ""})
        entry["arguments_json"] += str(event.get("delta") or "")
    elif etype == "response.output_item.done":
        item = event.get("item") or {}
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return
        idx = int(event.get("output_index") or len(tool_call_deltas))
        entry = tool_call_deltas.setdefault(idx, {"id": "", "name": "", "arguments_json": ""})
        entry["id"] = str(item.get("call_id") or item.get("id") or entry["id"])
        entry["name"] = str(item.get("name") or entry["name"])
        if item.get("arguments"):
            entry["arguments_json"] = str(item.get("arguments") or "")


def _build_tool_calls(tool_call_deltas: dict[int, dict]) -> list[dict]:
    tool_calls = []
    for idx in sorted(tool_call_deltas.keys()):
        block = tool_call_deltas[idx]
        try:
            inp = json.loads(block.get("arguments_json") or "{}")
        except json.JSONDecodeError:
            logger.warning("Failed to parse Responses tool call arguments for %s", block.get("name"))
            inp = {}
        tool_calls.append({
            "id": block.get("id", ""),
            "name": block.get("name", ""),
            "input": inp,
        })
    return tool_calls


def _sorted_tools(tools: list[dict]) -> list[dict]:
    return sorted(tools, key=lambda item: str(item.get("name") or ""))
