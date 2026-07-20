"""Generation state transitions and atomic candidate publication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from luna_agent.plugins.core.models import LoadedPlugin, PluginStatus
from luna_agent.plugins.runtime import CapabilityKind, PluginRuntimeState


_ALLOWED_TRANSITIONS = {
    PluginRuntimeState.DISCOVERED: {
        PluginRuntimeState.PREPARING,
        PluginRuntimeState.DRAINING,
        PluginRuntimeState.FAILED,
    },
    PluginRuntimeState.PREPARING: {
        PluginRuntimeState.ACTIVE,
        PluginRuntimeState.DRAINING,
        PluginRuntimeState.FAILED,
    },
    PluginRuntimeState.ACTIVE: {
        PluginRuntimeState.DRAINING,
        PluginRuntimeState.FAILED,
    },
    PluginRuntimeState.DRAINING: {
        PluginRuntimeState.PREPARING,
        PluginRuntimeState.ACTIVE,
        PluginRuntimeState.STOPPED,
        PluginRuntimeState.FAILED,
    },
    PluginRuntimeState.FAILED: {
        PluginRuntimeState.PREPARING,
        PluginRuntimeState.ACTIVE,
        PluginRuntimeState.DRAINING,
        PluginRuntimeState.STOPPED,
    },
    PluginRuntimeState.STOPPED: {PluginRuntimeState.PREPARING},
}


@dataclass(frozen=True)
class GenerationTransition:
    plugin_key: str
    runtime_instance_id: str
    previous: PluginRuntimeState
    current: PluginRuntimeState
    reason: str
    changed_at: str


@dataclass
class GenerationPublication:
    """Reversible cutover until the caller commits active execution state."""

    coordinator: "GenerationCoordinator"
    candidate: LoadedPlugin
    previous: LoadedPlugin
    previous_active_id: str
    transaction: Any
    data_commit: Any | None
    preserve_kinds: frozenset[CapabilityKind]
    finalized: bool = False
    rolled_back: bool = False

    def finalize(self) -> None:
        if self.finalized or self.rolled_back:
            return
        self.transaction.finalize()
        if self.data_commit is not None:
            self.data_commit.finalize()
        manager = self.coordinator.manager
        manager._record_boot_scope_pending(
            self.candidate,
            preserve_kinds=self.preserve_kinds,
        )
        self.finalized = True

    def rollback(self) -> None:
        if self.finalized or self.rolled_back:
            return
        manager = self.coordinator.manager
        manager._plugins[self.previous.key] = self.previous
        if self.previous_active_id:
            manager._active_runtime_by_plugin[self.previous.key] = (
                self.previous_active_id
            )
        else:
            manager._active_runtime_by_plugin.pop(self.previous.key, None)
        self.transaction.rollback()
        if self.data_commit is not None and not self.data_commit.finalized:
            self.data_commit.rollback()
        manager.capability_router.restore_owner(
            self.previous.key,
            self.previous.runtime_instance_id,
            preserve_kinds=self.preserve_kinds,
        )
        if self.previous.runtime_state is PluginRuntimeState.DRAINING:
            self.coordinator.transition(
                self.previous,
                PluginRuntimeState.ACTIVE,
                reason="candidate_publication_rolled_back",
            )
        self.rolled_back = True


class GenerationCoordinator:
    """Own state-machine writes and the registration/data/route commit boundary."""

    def __init__(self, manager) -> None:
        self.manager = manager
        self.transition_count = 0
        self.last_transition: GenerationTransition | None = None

    def transition(
        self,
        plugin: LoadedPlugin,
        target: PluginRuntimeState,
        *,
        reason: str = "",
    ) -> None:
        previous = plugin.runtime_state
        if previous is target:
            return
        if target not in _ALLOWED_TRANSITIONS.get(previous, set()):
            raise RuntimeError(
                f"invalid plugin generation transition for {plugin.key}: "
                f"{previous.value} -> {target.value}"
            )
        plugin.runtime_state = target
        self.transition_count += 1
        self.last_transition = GenerationTransition(
            plugin_key=plugin.key,
            runtime_instance_id=plugin.runtime_instance_id,
            previous=previous,
            current=target,
            reason=str(reason or ""),
            changed_at=datetime.now(UTC).isoformat(),
        )

    def publish_candidate(
        self,
        candidate: LoadedPlugin,
        previous: LoadedPlugin,
        *,
        data_commit=None,
    ) -> GenerationPublication:
        transaction = candidate.registration_transaction
        if transaction is None:
            raise RuntimeError(
                f"plugin registration transaction is unavailable: {candidate.key}"
            )
        previous_active_id = self.manager._active_runtime_by_plugin.get(candidate.key, "")
        preserve_kinds = self.manager.boot_scope_preserve_kinds(candidate.key)
        transaction.activate(preserve_kinds=preserve_kinds)
        route_published = False
        try:
            self.manager._plugins[candidate.key] = candidate
            self.manager._active_runtime_by_plugin[candidate.key] = (
                candidate.runtime_instance_id
            )
            candidate.status = PluginStatus.LOADED
            self.manager.capability_router.publish_staged(
                candidate.key,
                candidate.runtime_instance_id,
                preserve_kinds=preserve_kinds,
            )
            route_published = True
            self.transition(
                candidate,
                PluginRuntimeState.ACTIVE,
                reason="candidate_published",
            )
            self.transition(
                previous,
                PluginRuntimeState.DRAINING,
                reason="replaced_by_candidate",
            )
        except Exception:
            self.manager._plugins[previous.key] = previous
            if previous_active_id:
                self.manager._active_runtime_by_plugin[previous.key] = previous_active_id
            else:
                self.manager._active_runtime_by_plugin.pop(previous.key, None)
            transaction.rollback()
            if data_commit is not None and not data_commit.finalized:
                data_commit.rollback()
            if route_published:
                self.manager.capability_router.restore_owner(
                    previous.key,
                    previous.runtime_instance_id,
                    preserve_kinds=preserve_kinds,
                )
            raise
        return GenerationPublication(
            coordinator=self,
            candidate=candidate,
            previous=previous,
            previous_active_id=previous_active_id,
            transaction=transaction,
            data_commit=data_commit,
            preserve_kinds=preserve_kinds,
        )

    def health_snapshot(self) -> dict[str, object]:
        transition = self.last_transition
        return {
            "transition_count": self.transition_count,
            "last_transition": (
                {
                    "plugin_key": transition.plugin_key,
                    "runtime_instance_id": transition.runtime_instance_id,
                    "previous": transition.previous.value,
                    "current": transition.current.value,
                    "reason": transition.reason,
                    "changed_at": transition.changed_at,
                }
                if transition is not None
                else {}
            ),
        }
