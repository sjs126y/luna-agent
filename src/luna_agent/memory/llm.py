"""Dedicated facade for structured memory LLM calls."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

from luna_agent.llm.provider import provider_registry
from luna_agent.llm.transport_registry import transport_registry
from luna_agent.memory.config import MemoryLLMConfig
from luna_agent.memory.models import Observation
from luna_agent.memory.prompts import OBSERVATION_EXTRACTION_PROMPT, OBSERVATION_EXTRACTION_SYSTEM


class MemoryLLMFacade:
    def __init__(self, config: MemoryLLMConfig, *, transport=None) -> None:
        self.config = config
        self._owns_transport = transport is None
        self._transport = transport or self._create_transport(config)

    async def extract_observations(self, messages: list[dict[str, Any]]) -> tuple[Observation, ...]:
        conversation = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        payload = await self.call_json(
            system_prompt=OBSERVATION_EXTRACTION_SYSTEM,
            prompt=OBSERVATION_EXTRACTION_PROMPT.format(conversation=conversation),
        )
        values = payload.get("observations", [])
        if not isinstance(values, list):
            raise ValueError("Memory LLM observations must be a list")
        return tuple(Observation.from_dict(item) for item in values if isinstance(item, dict))

    async def call_json(self, *, system_prompt: str, prompt: str) -> dict[str, Any]:
        response = await self._transport.call(
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            system_prompt=system_prompt,
            tools=[],
            max_tokens=self.config.max_tokens,
        )
        text = str(getattr(response, "text", "") or "")
        data = _parse_json_object(text)
        if not isinstance(data, dict):
            raise ValueError("Memory LLM response must be a JSON object")
        return data

    async def close(self) -> None:
        if self._owns_transport:
            await self._transport.close()

    @staticmethod
    def _create_transport(config: MemoryLLMConfig):
        if not config.provider or not config.model:
            raise ValueError("Memory LLM provider and model are required")
        values = SimpleNamespace(
            llm_base_url=config.base_url,
            llm_api_key=config.api_key,
            llm_model=config.model,
            llm_max_tokens=config.max_tokens,
            llm_context_window=0,
            llm_reasoning_effort="",
            llm_api_mode=config.api_mode,
        )
        profile = provider_registry.get(config.provider, values)
        return transport_registry.get(profile.api_mode, profile)


def _parse_json_object(text: str) -> Any:
    value = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        value = fence.group(1)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start >= 0 and end > start:
            return json.loads(value[start:end + 1])
        raise
