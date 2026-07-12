"""App-owned asynchronous memory review workers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MemoryReviewJob:
    session_key: str
    user_id: str
    messages: list[dict[str, Any]]
    turn_id: str = ""


class MemoryReviewService:
    def __init__(self, memory_manager=None, *, interval: int = 10, concurrency: int = 2) -> None:
        self.memory_manager = memory_manager
        self.enabled = memory_manager is not None and interval > 0
        self.interval = interval
        self.concurrency = concurrency
        self.queue: asyncio.Queue[MemoryReviewJob] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._locks: dict[str, asyncio.Lock] = {}
        self.submitted = 0
        self.completed = 0
        self.skipped = 0
        self.maintenance_runs = 0
        self.migrations_completed = 0
        self.migrations_failed = 0
        self.last_error = ""

    async def start(self) -> None:
        if not self.enabled or self._tasks:
            return
        self._tasks = [asyncio.create_task(self._worker(), name=f"memory-review-{index}") for index in range(self.concurrency)]

    def submit(self, *, session_key: str, user_id: str, messages: list[dict], turn_id: str = "") -> bool:
        if not self.enabled or not messages:
            return False
        self.queue.put_nowait(MemoryReviewJob(session_key, user_id, list(messages), turn_id))
        self.submitted += 1
        return True

    async def _worker(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                self.queue.task_done()

    async def _process(self, job: MemoryReviewJob) -> None:
        lock = self._locks.setdefault(job.session_key, asyncio.Lock())
        async with lock:
            scope = self.memory_manager.scope(session_key=job.session_key, user_id=job.user_id)
            maintain = getattr(self.memory_manager, "maintain", None)
            if maintain is not None:
                maintenance = await maintain(scope, migration_limit=1)
                self.maintenance_runs += 1
                self.migrations_completed += int(maintenance.get("migration_completed") or 0)
                self.migrations_failed += int(maintenance.get("migration_failed") or 0)
            user_turns = [item for item in job.messages if item.get("role") == "user"]
            checkpoint = await self.memory_manager.archive.get_checkpoint(scope)
            reviewed = int(checkpoint["reviewed_turns"]) if checkpoint else 0
            if len(user_turns) - reviewed < self.interval:
                self.skipped += 1
                return
            pending_messages = _messages_after_user_turn(job.messages, reviewed)
            await self.memory_manager.review(
                pending_messages, scope, total_user_turns=len(user_turns)
            )
            await self.memory_manager.archive.set_checkpoint(
                scope, last_turn_id=job.turn_id, reviewed_turns=len(user_turns)
            )
            self.completed += 1
            self.last_error = ""

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "workers": len(self._tasks), "queue_size": self.queue.qsize(),
            "submitted": self.submitted, "completed": self.completed, "skipped": self.skipped,
            "maintenance_runs": self.maintenance_runs,
            "migrations_completed": self.migrations_completed,
            "migrations_failed": self.migrations_failed,
            "last_error": self.last_error,
        }

    async def close(self) -> None:
        if self._tasks:
            await self.queue.join()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()


def _messages_after_user_turn(messages: list[dict], reviewed_turns: int) -> list[dict]:
    seen = 0
    for index, message in enumerate(messages):
        if message.get("role") == "user":
            if seen >= reviewed_turns:
                return list(messages[index:])
            seen += 1
    return []
