from types import SimpleNamespace

import pytest

from luna_agent.memory.config import MemoryLLMConfig
from luna_agent.memory.llm import MemoryLLMFacade
from luna_agent.memory.models import ObservationKind


class Transport:
    async def call(self, **kwargs):
        return SimpleNamespace(text='```json\n{"observations":[{"kind":"preference","content":"likes tea"}]}\n```')


@pytest.mark.asyncio
async def test_memory_llm_extracts_structured_observations() -> None:
    config = MemoryLLMConfig("deepseek", "model", "url", "key", "chat_completions", 100)
    facade = MemoryLLMFacade(config, transport=Transport())

    result = await facade.extract_observations([{"role": "user", "content": "I like tea"}])

    assert result[0].kind == ObservationKind.PREFERENCE
    assert result[0].content == "likes tea"
