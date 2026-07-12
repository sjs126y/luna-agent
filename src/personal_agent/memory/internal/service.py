"""Persistent observation buffer and internal consolidation orchestration."""

from __future__ import annotations

from personal_agent.memory.internal.store import AUTO_FILES, InternalMemoryConflict, InternalMemoryStore
from personal_agent.memory.models import InternalPatchAction, MemoryScope, Observation


class InternalMemoryService:
    def __init__(self, *, archive, store: InternalMemoryStore, consolidator, buffer_limit: int = 20) -> None:
        self.archive = archive
        self.store = store
        self.consolidator = consolidator
        self.buffer_limit = buffer_limit

    async def enqueue(self, scope: MemoryScope, observations: tuple[Observation, ...], *, batch_id: str = "") -> int:
        await self.archive.save_observations(scope, observations, batch_id=batch_id)
        return await self.archive.add_to_internal_buffer(scope, observations)

    async def should_consolidate(self, scope: MemoryScope) -> bool:
        return await self.archive.pending_buffer_count(scope) >= self.buffer_limit

    async def consolidate(self, scope: MemoryScope, *, force: bool = False) -> dict[str, int]:
        observations = await self.archive.pending_buffer_observations(scope)
        if not observations or (not force and len(observations) < self.buffer_limit):
            return {"pending": len(observations), "applied": 0, "skipped": 0, "conflict": 0}
        snapshot = self.store.snapshot(profile=scope.profile)
        operations = await self.consolidator.propose(
            internal_content=snapshot.content,
            observations=observations,
        )
        auto_operations = [
            item for item in operations
            if item.action in {InternalPatchAction.ADD, InternalPatchAction.UPDATE}
            and item.target_file.upper() in AUTO_FILES
        ]
        try:
            if auto_operations:
                await self.store.apply_operations(snapshot, auto_operations)
        except InternalMemoryConflict:
            return {"pending": len(observations), "applied": 0, "skipped": 0, "conflict": 0}
        counts = {"pending": 0, "applied": 0, "skipped": 0, "conflict": 0}
        handled: set[str] = set()
        for operation in operations:
            handled.add(operation.observation_id)
            target = operation.target_file
            if operation.action == InternalPatchAction.SKIP:
                status = "skipped"
            elif operation.action == InternalPatchAction.CONFLICT or target.upper() not in AUTO_FILES:
                status = "conflict"
            else:
                status = "applied"
            await self.archive.set_buffer_status(
                operation.observation_id, status, target_file=target, reason=operation.reason
            )
            counts[status] += 1
        counts["pending"] = len(observations) - len(handled)
        return counts
