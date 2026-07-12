"""Select a plugin provider and fail over to the core SQLite provider."""

from __future__ import annotations

import inspect
from time import monotonic
from typing import Any

from personal_agent.memory.models import MemoryReviewResult, MemoryScope, ProviderReadiness


class ExternalMemoryRouter:
    def __init__(self, *, context, archive, fallback, registry) -> None:
        self.context = context
        self.archive = archive
        self.fallback = fallback
        self.registry = registry
        self.primary = None
        self.requested_provider = context.requested_provider
        self.effective_provider = "none" if self.requested_provider == "none" else fallback.name
        self.fallback_reason = ""
        self.last_primary_error = ""
        self._last_recovery_attempt = 0.0
        self._persisted_states: dict[tuple[str, str, str], tuple[str, str, str]] = {}

    async def initialize(self) -> None:
        if self.requested_provider in {"none", "fallback"}:
            self.effective_provider = self.requested_provider
            return
        registration = self.registry.get(self.requested_provider)
        if registration is None:
            self._use_fallback(f"provider not registered: {self.requested_provider}")
            return
        try:
            readiness = registration.validator(context=self.context)
            if inspect.isawaitable(readiness):
                readiness = await readiness
            if not isinstance(readiness, ProviderReadiness) or not readiness.available:
                self._use_fallback(getattr(readiness, "reason", "provider unavailable"))
                return
            provider = registration.factory(context=self.context, archive=self.archive)
            self.primary = await provider if inspect.isawaitable(provider) else provider
            self.effective_provider = self.requested_provider
            self.fallback_reason = ""
        except Exception as exc:
            self._use_fallback(f"{type(exc).__name__}: {exc}")

    async def review(self, messages: list[dict[str, Any]], scope: MemoryScope) -> MemoryReviewResult:
        await self._prepare_scope(scope)
        provider = self._effective()
        if provider is None:
            return MemoryReviewResult(provider="none")
        try:
            result = await provider.review(messages, scope)
            await self._persist_state(scope)
            return result
        except Exception as exc:
            if provider is self.fallback:
                raise
            self.last_primary_error = f"{type(exc).__name__}: {exc}"
            self._use_fallback(self.last_primary_error)
            await self._persist_state(scope)
            result = await self.fallback.review(messages, scope)
            await self._persist_state(scope)
            return result

    async def search(self, query: str, scope: MemoryScope, *, limit: int = 5):
        await self._prepare_scope(scope)
        provider = self._effective()
        if provider is None:
            return []
        try:
            result = await provider.search(query, scope, limit=limit)
            await self._persist_state(scope)
            return result
        except Exception as exc:
            if provider is self.fallback:
                raise
            self.last_primary_error = f"{type(exc).__name__}: {exc}"
            self._use_fallback(self.last_primary_error)
            await self._persist_state(scope)
            return await self.fallback.search(query, scope, limit=limit)

    async def list(self, scope: MemoryScope, *, limit: int = 100):
        await self._prepare_scope(scope)
        provider = self._effective()
        if provider is None:
            return []
        try:
            result = await provider.list(scope, limit=limit)
            await self._persist_state(scope)
            return result
        except Exception as exc:
            if provider is self.fallback:
                raise
            self.last_primary_error = f"{type(exc).__name__}: {exc}"
            self._use_fallback(self.last_primary_error)
            await self._persist_state(scope)
            return await self.fallback.list(scope, limit=limit)

    async def delete(self, memory_id: str, scope: MemoryScope) -> bool:
        await self._prepare_scope(scope)
        provider = self._effective()
        if provider is None:
            return False
        try:
            result = await provider.delete(memory_id, scope)
            await self._persist_state(scope)
            return result
        except Exception as exc:
            if provider is self.fallback:
                raise
            self.last_primary_error = f"{type(exc).__name__}: {exc}"
            self._use_fallback(self.last_primary_error)
            await self._persist_state(scope)
            return await self.fallback.delete(memory_id, scope)

    async def history(self, memory_id: str):
        provider = self._effective()
        return [] if provider is None else await provider.history(memory_id)

    async def migrate(self, observations, scope: MemoryScope):
        await self._prepare_scope(scope)
        provider = self._effective()
        if provider is None:
            return MemoryReviewResult(observations=tuple(observations), provider="none")
        try:
            result = await provider.migrate(tuple(observations), scope)
            await self._persist_state(scope)
            return result
        except Exception as exc:
            if provider is self.fallback:
                raise
            self.last_primary_error = f"{type(exc).__name__}: {exc}"
            self._use_fallback(self.last_primary_error)
            await self._persist_state(scope)
            return await self.fallback.migrate(tuple(observations), scope)

    def health_snapshot(self) -> dict[str, Any]:
        provider = self._effective()
        return {
            "requested_provider": self.requested_provider,
            "effective_provider": self.effective_provider,
            "fallback_reason": self.fallback_reason,
            "last_primary_error": self.last_primary_error,
            "provider": provider.health_snapshot() if provider else {"available": False},
        }

    async def close(self) -> None:
        if self.primary is not None:
            await self.primary.close()
        await self.fallback.close()

    async def maybe_recover(self, scope: MemoryScope, *, cooldown_seconds: float = 300) -> bool:
        if self.effective_provider != self.fallback.name or self.requested_provider in {"none", "fallback"}:
            return False
        now = monotonic()
        if now - self._last_recovery_attempt < cooldown_seconds:
            return False
        self._last_recovery_attempt = now
        registration = self.registry.get(self.requested_provider)
        if registration is None:
            return False
        try:
            readiness = registration.validator(context=self.context)
            if inspect.isawaitable(readiness):
                readiness = await readiness
            if not readiness.available:
                self.fallback_reason = readiness.reason
                return False
            candidate = registration.factory(context=self.context, archive=self.archive)
            candidate = await candidate if inspect.isawaitable(candidate) else candidate
            pending = tuple(await self.archive.pending_migration_observations(scope))
            if pending:
                await candidate.migrate(pending, scope)
                await self.archive.mark_observations_migrated([item.id for item in pending])
            old_primary = self.primary
            self.primary = candidate
            self.effective_provider = self.requested_provider
            self.fallback_reason = ""
            self.last_primary_error = ""
            if old_primary is not None and old_primary is not candidate:
                await old_primary.close()
            return True
        except Exception as exc:
            self.last_primary_error = f"{type(exc).__name__}: {exc}"
            self.fallback_reason = self.last_primary_error
            return False

    def _effective(self):
        if self.effective_provider == "none":
            return None
        return self.primary if self.effective_provider == self.requested_provider else self.fallback

    def _use_fallback(self, reason: str) -> None:
        self.effective_provider = self.fallback.name
        self.fallback_reason = reason or "provider unavailable"

    async def _prepare_scope(self, scope: MemoryScope) -> None:
        await self.maybe_recover(scope)
        await self._persist_state(scope)

    async def _persist_state(self, scope: MemoryScope) -> None:
        scope_key = (scope.user_id, scope.session_key, scope.profile)
        state = (self.requested_provider, self.effective_provider, self.fallback_reason)
        if self._persisted_states.get(scope_key) == state:
            return
        await self.archive.set_provider_state(
            scope,
            requested=self.requested_provider,
            effective=self.effective_provider,
            fallback_reason=self.fallback_reason,
        )
        self._persisted_states[scope_key] = state
