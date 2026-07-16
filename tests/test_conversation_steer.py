"""Runtime steer state manager."""

from __future__ import annotations

import asyncio

import pytest

from personal_agent.conversation.steer import ActiveTurnRegistry, SteerManager


def test_steer_manager_binds_pending_signal_to_started_turn():
    manager = SteerManager()

    signal = manager.add(" session ", None, " answer shorter ")

    assert signal.session_key == "session"
    assert signal.text == "answer shorter"
    assert signal.turn_id == ""

    manager.begin_turn("session", "turn-1")

    assert signal.turn_id == "turn-1"
    consumed = manager.consume("session", "turn-1")
    assert [item.id for item in consumed] == [signal.id]
    assert signal.status == "consumed"
    assert signal.consumed_at > 0
    assert manager.snapshot("session")["pending_count"] == 0
    assert manager.turn_summary("session", "turn-1")["consumed"] == 1


def test_steer_manager_expires_unconsumed_current_turn_signals():
    manager = SteerManager()
    manager.begin_turn("session", "turn-1")
    signal = manager.add("session", None, "focus on tests")

    expired = manager.end_turn("session", "turn-1")

    assert [item.id for item in expired] == [signal.id]
    assert signal.status == "expired"
    assert manager.snapshot("session")["active_turn_id"] == ""
    summary = manager.turn_summary("session", "turn-1")
    assert summary["received"] == 1
    assert summary["expired"] == 1


def test_steer_manager_pending_limit_expires_oldest_signal():
    manager = SteerManager(max_pending_per_session=2)
    manager.begin_turn("session", "turn-1")

    first = manager.add("session", None, "first")
    second = manager.add("session", None, "second")
    third = manager.add("session", None, "third")

    snapshot = manager.snapshot("session")
    assert first.status == "expired"
    assert snapshot["pending_count"] == 2
    assert [item["id"] for item in snapshot["pending_items"]] == [second.id, third.id]


def test_active_turn_registry_only_accepts_steer_for_active_turn():
    registry = ActiveTurnRegistry()

    with pytest.raises(RuntimeError, match="no active turn"):
        registry.add("session", None, "too early")

    active = registry.begin_turn("session", "turn-1", request_id="sub-1")
    signal = registry.add("session", None, "focus")

    assert active.request_id == "sub-1"
    assert signal.turn_id == "turn-1"
    assert registry.consume("session", "turn-1") == [signal]
    registry.end_turn("session", "turn-1")
    assert registry.active_turn("session") is None


@pytest.mark.asyncio
async def test_active_turn_registry_can_cancel_registered_task():
    registry = ActiveTurnRegistry()

    async def wait_forever():
        registry.begin_turn("session", "turn-1", task=asyncio.current_task())
        await asyncio.Event().wait()

    task = asyncio.create_task(wait_forever())
    await asyncio.sleep(0)

    assert registry.cancel("session") is True
    with pytest.raises(asyncio.CancelledError):
        await task
    registry.end_turn("session", "turn-1")
