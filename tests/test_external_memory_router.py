from types import SimpleNamespace

import pytest

from personal_agent.memory.archive import MemoryArchive
from personal_agent.memory.external import ExternalMemoryRouter, FallbackMemoryProvider
from personal_agent.memory.models import (
    MemoryReviewResult,
    MemoryScope,
    Observation,
    ObservationKind,
    ProviderReadiness,
)
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


class HealthyProvider:
    name = "primary"

    async def review(self, messages, scope):
        return MemoryReviewResult(provider=self.name)

    async def search(self, query, scope, *, limit=5):
        return []

    async def list(self, scope, *, limit=100):
        return []

    async def delete(self, memory_id, scope):
        return False

    async def history(self, memory_id):
        return []

    async def migrate(self, observations, scope):
        return MemoryReviewResult(observations=tuple(observations), provider=self.name)

    def health_snapshot(self):
        return {"available": True}

    async def close(self):
        pass


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
    state = await archive._fetchone(
        "SELECT effective_provider,fallback_reason FROM provider_state LIMIT 1"
    )
    assert state["effective_provider"] == "fallback"
    assert "primary failed" in state["fallback_reason"]
    await archive.close()


@pytest.mark.asyncio
async def test_router_recovers_before_foreground_migration(tmp_path) -> None:
    archive = MemoryArchive(tmp_path / "memory.db")
    await archive.initialize()
    fallback = FallbackMemoryProvider(archive, LLM())
    registry = MemoryProviderRegistry()
    available = {"value": False}
    registry.register(
        name="primary",
        plugin_key="memory/primary",
        factory=lambda **kwargs: HealthyProvider(),
        validator=lambda **kwargs: ProviderReadiness(
            "primary", available["value"], "dependency unavailable"
        ),
    )
    context = SimpleNamespace(requested_provider="primary")
    router = ExternalMemoryRouter(context=context, archive=archive, fallback=fallback, registry=registry)
    await router.initialize()
    assert router.effective_provider == "fallback"

    available["value"] = True
    scope = MemoryScope(user_id="u1")
    observation = Observation(kind=ObservationKind.EVENT, content="runtime recovered")
    result = await router.migrate((observation,), scope)

    assert result.provider == "primary"
    assert router.effective_provider == "primary"
    assert router.fallback_reason == ""
    state = await archive._fetchone(
        "SELECT effective_provider,fallback_reason FROM provider_state LIMIT 1"
    )
    assert state["effective_provider"] == "primary"
    assert state["fallback_reason"] == ""
    await archive.close()
