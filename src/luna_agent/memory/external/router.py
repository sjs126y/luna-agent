"""Select a plugin provider and fail over to the core SQLite provider."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import logging
from time import monotonic
from typing import Any

from luna_agent.memory.models import MemoryReviewResult, MemoryScope, ProviderReadiness, utc_now

logger = logging.getLogger(__name__)


@dataclass
class ScopeRouteState:
    effective_provider: str
    fallback_reason: str = ""
    last_primary_error: str = ""
    consecutive_failures: int = 0
    last_recovery_attempt: float = 0.0
    last_probe_at: str = ""
    last_probe_status: str = "not_run"


class ExternalMemoryRouter:
    def __init__(self, *, context, archive, fallback, registry) -> None:
        self.context = context
        self.archive = archive
        self.fallback = fallback
        self.registry = registry
        self.primary = None
        self._retired_primaries: list[Any] = []
        self._recovery_lock = asyncio.Lock()
        self.requested_provider = context.requested_provider
        self._initial_effective_provider = (
            "none" if self.requested_provider == "none" else fallback.name
        )
        self._initial_fallback_reason = ""
        self._states: dict[tuple[str, str, str], ScopeRouteState] = {}
        self._last_scope_key: tuple[str, str, str] | None = None
        self._persisted_states: dict[tuple[str, str, str], tuple[Any, ...]] = {}

    @property
    def effective_provider(self) -> str:
        return self._last_state().effective_provider

    @property
    def fallback_reason(self) -> str:
        return self._last_state().fallback_reason

    @property
    def last_primary_error(self) -> str:
        return self._last_state().last_primary_error

    async def initialize(self) -> None:
        if self.requested_provider in {"none", "fallback"}:
            self._initial_effective_provider = self.requested_provider
            return
        registration = self.registry.get(self.requested_provider)
        if registration is None:
            self._set_initial_fallback(f"provider not registered: {self.requested_provider}")
            return
        try:
            readiness = registration.validator(context=self.context)
            if inspect.isawaitable(readiness):
                readiness = await readiness
            if not isinstance(readiness, ProviderReadiness) or not readiness.available:
                self._set_initial_fallback(getattr(readiness, "reason", "provider unavailable"))
                return
            provider = registration.factory(context=self.context, archive=self.archive)
            self.primary = await provider if inspect.isawaitable(provider) else provider
            self._initial_effective_provider = self.requested_provider
            self._initial_fallback_reason = ""
        except Exception as exc:
            self._set_initial_fallback(_exception_detail(exc))

    async def review(self, messages: list[dict[str, Any]], scope: MemoryScope) -> MemoryReviewResult:
        state = await self._prepare_scope(scope)
        provider = self._effective(state)
        if provider is None:
            return MemoryReviewResult(provider="none")
        try:
            result = await provider.review(messages, scope)
            self._record_success(state)
            await self._persist_state(scope, state)
            return result
        except Exception as exc:
            if provider is self.fallback:
                raise
            self._record_failure(state, exc, use_fallback=True)
            await self._persist_state(scope, state)
            result = await self.fallback.review(messages, scope)
            await self._persist_state(scope, state)
            return result

    async def search(self, query: str, scope: MemoryScope, *, limit: int = 5):
        state = await self._prepare_scope(scope)
        provider = self._effective(state)
        if provider is None:
            return []
        try:
            if provider is self.fallback:
                return await provider.search(query, scope, limit=limit)
            result = await self._retry_primary_search(provider, query, scope, limit=limit)
            self._record_success(state)
            await self._persist_state(scope, state)
            return result
        except Exception as exc:
            if provider is self.fallback:
                raise
            self._record_failure(state, exc, use_fallback=True)
            await self._persist_state(scope, state)
            return await self.fallback.search(query, scope, limit=limit)

    async def list(self, scope: MemoryScope, *, limit: int = 100):
        state = await self._prepare_scope(scope)
        provider = self._effective(state)
        if provider is None:
            return []
        try:
            result = await provider.list(scope, limit=limit)
            if provider is not self.fallback:
                self._record_success(state)
            await self._persist_state(scope, state)
            return result
        except Exception as exc:
            if provider is self.fallback:
                raise
            self._record_failure(state, exc, use_fallback=True)
            await self._persist_state(scope, state)
            return await self.fallback.list(scope, limit=limit)

    async def delete(self, memory_id: str, scope: MemoryScope) -> bool:
        state = await self._prepare_scope(scope)
        provider = self._effective(state)
        if provider is None:
            return False
        try:
            result = await provider.delete(memory_id, scope)
            if provider is not self.fallback:
                self._record_success(state)
            await self._persist_state(scope, state)
            return result
        except Exception as exc:
            if provider is self.fallback:
                raise
            self._record_failure(state, exc, use_fallback=True)
            await self._persist_state(scope, state)
            return await self.fallback.delete(memory_id, scope)

    async def history(self, memory_id: str, scope: MemoryScope | None = None):
        state = self._state(scope) if scope is not None else self._last_state()
        provider = self._effective(state)
        return [] if provider is None else await provider.history(memory_id)

    async def migrate(self, observations, scope: MemoryScope):
        state = await self._prepare_scope(scope)
        provider = self._effective(state)
        if provider is None:
            return MemoryReviewResult(observations=tuple(observations), provider="none")
        try:
            result = await provider.migrate(tuple(observations), scope)
            if provider is not self.fallback:
                self._record_success(state)
            await self._persist_state(scope, state)
            return result
        except Exception as exc:
            if provider is self.fallback:
                raise
            self._record_failure(state, exc, use_fallback=True)
            await self._persist_state(scope, state)
            return await self.fallback.migrate(tuple(observations), scope)

    def health_snapshot(self, scope: MemoryScope | None = None) -> dict[str, Any]:
        state = self._state(scope) if scope is not None else self._last_state()
        provider = self._effective(state)
        return {
            "requested_provider": self.requested_provider,
            "effective_provider": state.effective_provider,
            "fallback_reason": state.fallback_reason,
            "last_primary_error": state.last_primary_error,
            "consecutive_failures": state.consecutive_failures,
            "last_probe_at": state.last_probe_at,
            "last_probe_status": state.last_probe_status,
            "provider": provider.health_snapshot() if provider else {"available": False},
        }

    def effective_provider_for(self, scope: MemoryScope) -> str:
        return self._state(scope).effective_provider

    async def close(self) -> None:
        if self.primary is not None:
            await self.primary.close()
        for provider in self._retired_primaries:
            await provider.close()
        self._retired_primaries.clear()
        await self.fallback.close()

    async def maybe_recover(self, scope: MemoryScope, *, cooldown_seconds: float = 300) -> bool:
        async with self._recovery_lock:
            return await self._maybe_recover_locked(scope, cooldown_seconds=cooldown_seconds)

    async def _maybe_recover_locked(
        self,
        scope: MemoryScope,
        *,
        cooldown_seconds: float,
    ) -> bool:
        state = self._state(scope)
        if state.effective_provider != self.fallback.name or self.requested_provider in {"none", "fallback"}:
            return False
        now = monotonic()
        if now - state.last_recovery_attempt < cooldown_seconds:
            return False
        state.last_recovery_attempt = now
        registration = self.registry.get(self.requested_provider)
        if registration is None:
            return False
        candidate = None
        try:
            readiness = registration.validator(context=self.context)
            if inspect.isawaitable(readiness):
                readiness = await readiness
            if not readiness.available:
                state.fallback_reason = readiness.reason
                return False
            candidate = registration.factory(context=self.context, archive=self.archive)
            candidate = await candidate if inspect.isawaitable(candidate) else candidate
            await self._probe_provider(candidate, scope)
            state.last_probe_at = utc_now()
            state.last_probe_status = "ok"
            old_primary = self.primary
            self.primary = candidate
            state.effective_provider = self.requested_provider
            state.fallback_reason = ""
            state.last_primary_error = ""
            state.consecutive_failures = 0
            if old_primary is not None and old_primary is not candidate:
                self._retired_primaries.append(old_primary)
            return True
        except Exception as exc:
            state.last_probe_at = utc_now()
            state.last_probe_status = "error"
            self._record_failure(state, exc, use_fallback=True)
            if candidate is not None and candidate is not self.primary:
                try:
                    await candidate.close()
                except Exception:
                    logger.exception("Failed to close rejected memory provider candidate")
            return False

    async def maintain(
        self,
        scope: MemoryScope,
        *,
        migration_limit: int = 1,
        index_limit: int = 1,
    ) -> dict[str, Any]:
        state = await self._prepare_scope(scope)
        result = {
            "effective_provider": state.effective_provider,
            "migration_attempted": 0,
            "migration_completed": 0,
            "migration_failed": 0,
            "index_attempted": 0,
            "index_completed": 0,
            "index_failed": 0,
        }
        if state.effective_provider != self.requested_provider or self.primary is None:
            return result
        pending = await self.archive.pending_migration_observations(
            scope, limit=max(0, migration_limit)
        )
        for observation in pending:
            result["migration_attempted"] += 1
            try:
                await self.primary.migrate((observation,), scope)
            except Exception as exc:
                detail = _exception_detail(exc)
                state.last_primary_error = detail
                state.consecutive_failures += 1
                await self.archive.mark_observation_migration_failed(observation.id, detail)
                result["migration_failed"] += 1
                logger.warning("External memory migration deferred: %s", detail)
                break
            await self.archive.mark_observations_migrated([observation.id])
            result["migration_completed"] += 1
        pending_reader = getattr(self.primary, "pending_reindex_records", None)
        if pending_reader is not None:
            pending_index = await pending_reader(scope, limit=max(0, index_limit))
        else:
            pending_index = await self.archive.pending_index_memories(
                scope, limit=max(0, index_limit)
            )
        reindex = getattr(self.primary, "reindex", None)
        if pending_index and reindex is not None:
            indexed = await reindex(pending_index, scope)
            result["index_attempted"] += int(indexed.get("attempted") or 0)
            result["index_completed"] += int(indexed.get("completed") or 0)
            result["index_failed"] += int(indexed.get("failed") or 0)
        await self._persist_state(scope, state)
        return result

    async def reindex_all(self, *, index_kind: str = "all", limit: int = 100000) -> dict[str, Any]:
        if self.primary is None or self.requested_provider != "luna":
            raise RuntimeError("Luna memory provider is not active")
        reindex = getattr(self.primary, "reindex_all", None)
        if reindex is None:
            raise RuntimeError(f"Memory provider does not support full reindex: {self.requested_provider}")
        return await reindex(index_kind=index_kind, limit=limit)

    async def probe(self, scope: MemoryScope) -> dict[str, Any]:
        state = self._state(scope)
        if self.requested_provider in {"none", "fallback"}:
            state.last_probe_at = utc_now()
            state.last_probe_status = "skipped"
            await self._persist_state(scope, state)
            return self.health_snapshot(scope)
        if state.effective_provider == self.fallback.name:
            await self.maybe_recover(scope, cooldown_seconds=0)
            await self._persist_state(scope, state)
            return self.health_snapshot(scope)
        try:
            await self._probe_provider(self.primary, scope)
            state.last_probe_at = utc_now()
            state.last_probe_status = "ok"
            self._record_success(state)
        except Exception as exc:
            state.last_probe_at = utc_now()
            state.last_probe_status = "error"
            self._record_failure(state, exc, use_fallback=True)
        await self._persist_state(scope, state)
        return self.health_snapshot(scope)

    async def _prepare_scope(self, scope: MemoryScope) -> ScopeRouteState:
        state = self._state(scope)
        await self.maybe_recover(scope)
        await self._persist_state(scope, state)
        return state

    async def _persist_state(self, scope: MemoryScope, state: ScopeRouteState) -> None:
        scope_key = _scope_state_key(scope)
        persisted = (
            self.requested_provider,
            state.effective_provider,
            state.fallback_reason,
            state.consecutive_failures,
            state.last_probe_at,
            state.last_probe_status,
        )
        if self._persisted_states.get(scope_key) == persisted:
            return
        await self.archive.set_provider_state(
            scope,
            requested=self.requested_provider,
            effective=state.effective_provider,
            fallback_reason=state.fallback_reason,
            state={
                "last_primary_error": state.last_primary_error,
                "consecutive_failures": state.consecutive_failures,
                "last_probe_at": state.last_probe_at,
                "last_probe_status": state.last_probe_status,
            },
        )
        self._persisted_states[scope_key] = persisted

    async def _retry_primary_search(self, provider, query: str, scope: MemoryScope, *, limit: int):
        try:
            return await provider.search(query, scope, limit=limit)
        except Exception as first_error:
            logger.warning("External memory search failed, retrying once: %s", _exception_detail(first_error))
            try:
                return await provider.search(query, scope, limit=limit)
            except Exception as second_error:
                raise second_error from first_error

    async def _probe_provider(self, provider, scope: MemoryScope) -> None:
        probe = getattr(provider, "probe", None)
        try:
            if probe is not None:
                await probe(scope)
            else:
                await provider.search("memory provider health probe", scope, limit=1)
        except Exception as first_error:
            logger.warning("External memory probe failed, retrying once: %s", _exception_detail(first_error))
            try:
                if probe is not None:
                    await probe(scope)
                else:
                    await provider.search("memory provider health probe", scope, limit=1)
            except Exception as second_error:
                raise second_error from first_error

    def _state(self, scope: MemoryScope | None) -> ScopeRouteState:
        if scope is None:
            return self._last_state()
        key = _scope_state_key(scope)
        self._last_scope_key = key
        state = self._states.get(key)
        if state is None:
            state = ScopeRouteState(
                effective_provider=self._initial_effective_provider,
                fallback_reason=self._initial_fallback_reason,
            )
            self._states[key] = state
        return state

    def _last_state(self) -> ScopeRouteState:
        if self._last_scope_key is not None and self._last_scope_key in self._states:
            return self._states[self._last_scope_key]
        return ScopeRouteState(
            effective_provider=self._initial_effective_provider,
            fallback_reason=self._initial_fallback_reason,
        )

    def _effective(self, state: ScopeRouteState):
        if state.effective_provider == "none":
            return None
        return self.primary if state.effective_provider == self.requested_provider else self.fallback

    def _record_success(self, state: ScopeRouteState) -> None:
        state.consecutive_failures = 0
        state.last_primary_error = ""
        if state.effective_provider == self.requested_provider:
            state.fallback_reason = ""

    def _record_failure(self, state: ScopeRouteState, exc: BaseException, *, use_fallback: bool) -> None:
        detail = _exception_detail(exc)
        state.last_primary_error = detail
        state.consecutive_failures += 1
        if use_fallback:
            state.effective_provider = self.fallback.name
            state.fallback_reason = detail

    def _set_initial_fallback(self, reason: str) -> None:
        self._initial_effective_provider = self.fallback.name
        self._initial_fallback_reason = reason or "provider unavailable"


def _scope_state_key(scope: MemoryScope) -> tuple[str, str, str]:
    return (scope.user_id, scope.session_key, scope.profile)


def _exception_detail(exc: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).strip()
        label = type(current).__name__
        parts.append(f"{label}: {text}" if text else f"{label}: {current!r}")
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)
