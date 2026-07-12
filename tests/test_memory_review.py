"""App-owned asynchronous memory review workers."""

from __future__ import annotations

import pytest

from personal_agent.memory.models import MemoryScope
from personal_agent.memory.review import MemoryReviewService, _messages_after_user_turn


class Archive:
    def __init__(self):
        self.checkpoint = None

    async def get_checkpoint(self, scope):
        return self.checkpoint

    async def set_checkpoint(self, scope, *, last_turn_id, reviewed_turns):
        self.checkpoint = {"last_turn_id": last_turn_id, "reviewed_turns": reviewed_turns}


class Manager:
    def __init__(self, *, error=None):
        self.archive = Archive()
        self.reviews = []
        self.error = error

    def scope(self, *, session_key, user_id):
        return MemoryScope(user_id=user_id, session_key=session_key)

    async def review(self, messages, scope):
        if self.error:
            raise self.error
        self.reviews.append((messages, scope))


def _messages(turns: int) -> list[dict]:
    result = []
    for index in range(turns):
        result.extend([
            {"role": "user", "content": f"u{index}"},
            {"role": "assistant", "content": f"a{index}"},
        ])
    return result


@pytest.mark.asyncio
async def test_review_worker_uses_interval_and_persists_checkpoint() -> None:
    manager = Manager()
    service = MemoryReviewService(manager, interval=2, concurrency=1)
    await service.start()

    assert service.submit(session_key="cli:1", user_id="u1", messages=_messages(2), turn_id="t2")
    await service.queue.join()

    assert len(manager.reviews) == 1
    assert len(manager.reviews[0][0]) == 4
    assert manager.archive.checkpoint == {"last_turn_id": "t2", "reviewed_turns": 2}
    assert service.health_snapshot()["completed"] == 1
    await service.close()
    assert service.health_snapshot()["workers"] == 0


@pytest.mark.asyncio
async def test_review_worker_skips_until_enough_new_turns() -> None:
    manager = Manager()
    manager.archive.checkpoint = {"last_turn_id": "t2", "reviewed_turns": 2}
    service = MemoryReviewService(manager, interval=2, concurrency=1)
    await service.start()

    service.submit(session_key="cli:1", user_id="u1", messages=_messages(3), turn_id="t3")
    await service.queue.join()

    assert manager.reviews == []
    assert service.health_snapshot()["skipped"] == 1
    await service.close()


@pytest.mark.asyncio
async def test_review_worker_records_errors_without_dying() -> None:
    manager = Manager(error=RuntimeError("boom"))
    service = MemoryReviewService(manager, interval=1, concurrency=1)
    await service.start()
    service.submit(session_key="cli:1", user_id="u1", messages=_messages(1))
    await service.queue.join()

    assert service.health_snapshot()["last_error"] == "RuntimeError: boom"
    assert service.health_snapshot()["workers"] == 1
    await service.close()


def test_messages_after_user_turn_keeps_unreviewed_suffix() -> None:
    assert _messages_after_user_turn(_messages(3), 2) == _messages(3)[4:]
