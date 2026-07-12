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


class FlakySearchProvider(HealthyProvider):
    def __init__(self, failures_by_user):
        self.failures_by_user = failures_by_user
        self.calls_by_user = {}

    async def search(self, query, scope, *, limit=5):
        calls = self.calls_by_user.get(scope.user_id, 0) + 1
        self.calls_by_user[scope.user_id] = calls
        if calls <= self.failures_by_user.get(scope.user_id, 0):
            try:
                raise RuntimeError("connection reset")
            except RuntimeError as cause:
                raise ResponseHandlingException() from cause
        return []


class ResponseHandlingException(RuntimeError):
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


@pytest.mark.asyncio
async def test_router_retries_transient_search_without_fallback(tmp_path) -> None:
    archive = MemoryArchive(tmp_path / "memory.db")
    await archive.initialize()
    fallback = FallbackMemoryProvider(archive, LLM())
    registry = MemoryProviderRegistry()
    provider = FlakySearchProvider({"u1": 1})
    registry.register(
        name="primary", plugin_key="memory/primary",
        factory=lambda **kwargs: provider,
        validator=lambda **kwargs: ProviderReadiness("primary", True),
    )
    router = ExternalMemoryRouter(
        context=SimpleNamespace(requested_provider="primary"),
        archive=archive,
        fallback=fallback,
        registry=registry,
    )
    await router.initialize()
    scope = MemoryScope(user_id="u1")

    assert await router.search("probe", scope) == []
    assert provider.calls_by_user["u1"] == 2
    assert router.health_snapshot(scope)["effective_provider"] == "primary"
    assert router.health_snapshot(scope)["consecutive_failures"] == 0
    await archive.close()


@pytest.mark.asyncio
async def test_router_fallback_is_isolated_by_scope_and_preserves_cause(tmp_path) -> None:
    archive = MemoryArchive(tmp_path / "memory.db")
    await archive.initialize()
    fallback = FallbackMemoryProvider(archive, LLM())
    registry = MemoryProviderRegistry()
    provider = FlakySearchProvider({"u1": 2})
    registry.register(
        name="primary", plugin_key="memory/primary",
        factory=lambda **kwargs: provider,
        validator=lambda **kwargs: ProviderReadiness("primary", True),
    )
    router = ExternalMemoryRouter(
        context=SimpleNamespace(requested_provider="primary"),
        archive=archive,
        fallback=fallback,
        registry=registry,
    )
    await router.initialize()
    failed_scope = MemoryScope(user_id="u1")
    healthy_scope = MemoryScope(user_id="u2")

    assert await router.search("probe", failed_scope) == []
    assert router.health_snapshot(failed_scope)["effective_provider"] == "fallback"
    reason = router.health_snapshot(failed_scope)["fallback_reason"]
    assert "ResponseHandlingException" in reason
    assert "connection reset" in reason

    assert await router.search("probe", healthy_scope) == []
    assert router.health_snapshot(healthy_scope)["effective_provider"] == "primary"
    assert router.health_snapshot(failed_scope)["effective_provider"] == "fallback"
    await archive.close()


@pytest.mark.asyncio
async def test_recovery_does_not_block_on_pending_migrations(tmp_path) -> None:
    archive = MemoryArchive(tmp_path / "memory.db")
    await archive.initialize()
    fallback = FallbackMemoryProvider(archive, LLM())
    scope = MemoryScope(user_id="u1")
    observation = Observation(kind=ObservationKind.EVENT, content="pending item")
    await fallback.migrate((observation,), scope)
    registry = MemoryProviderRegistry()
    available = {"value": False}
    provider = HealthyProvider()
    registry.register(
        name="primary", plugin_key="memory/primary",
        factory=lambda **kwargs: provider,
        validator=lambda **kwargs: ProviderReadiness(
            "primary", available["value"], "dependency unavailable"
        ),
    )
    router = ExternalMemoryRouter(
        context=SimpleNamespace(requested_provider="primary"),
        archive=archive,
        fallback=fallback,
        registry=registry,
    )
    await router.initialize()

    available["value"] = True
    assert await router.maybe_recover(scope, cooldown_seconds=0) is True
    assert router.health_snapshot(scope)["effective_provider"] == "primary"
    assert (await archive.migration_status_counts(scope))["pending"] == 1

    maintenance = await router.maintain(scope, migration_limit=1)
    assert maintenance["migration_completed"] == 1
    assert (await archive.migration_status_counts(scope))["migrated"] == 1
    await archive.close()
