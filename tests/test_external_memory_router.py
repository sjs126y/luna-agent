from types import SimpleNamespace

import pytest

from personal_agent.memory.archive import MemoryArchive
from personal_agent.memory.external import ExternalMemoryRouter, FallbackMemoryProvider
from personal_agent.memory.models import MemoryReviewResult, MemoryScope, ProviderReadiness
from personal_agent.memory.provider_registry import MemoryProviderRegistry


class LLM:
    async def extract_observations(self, messages):
        return ()


class BrokenProvider:
    name = "primary"

    async def review(self, messages, scope):
        raise RuntimeError("primary failed")

    def health_snapshot(self):
        return {"available": True}

    async def close(self):
        pass

    async def migrate(self, observations, scope):
        return MemoryReviewResult(observations=observations, provider=self.name)


@pytest.mark.asyncio
async def test_router_falls_back_on_runtime_failure(tmp_path) -> None:
    archive = MemoryArchive(tmp_path / "memory.db")
    await archive.initialize()
    fallback = FallbackMemoryProvider(archive, LLM())
    registry = MemoryProviderRegistry()
    registry.register(
        name="primary", plugin_key="memory/primary",
        factory=lambda **kwargs: BrokenProvider(),
        validator=lambda **kwargs: ProviderReadiness("primary", True),
    )
    context = SimpleNamespace(requested_provider="primary")
    router = ExternalMemoryRouter(context=context, archive=archive, fallback=fallback, registry=registry)
    await router.initialize()

    result = await router.review([], MemoryScope(user_id="u1"))

    assert result == MemoryReviewResult(provider="fallback", batch_id=result.batch_id)
    assert router.effective_provider == "fallback"
    assert "primary failed" in router.fallback_reason
    await archive.close()
