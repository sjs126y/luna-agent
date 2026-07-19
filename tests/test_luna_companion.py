from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from plugins.luna_companion.luna_companion import (
    CompanionConfig,
    CompanionRunner,
    CompanionStore,
    _activation,
    _in_quiet_hours,
)
from luna_agent.plugins.core.ports import PluginStoragePort
from luna_agent.plugins.runtime import PluginRuntimeState


def _plugin(tmp_path):
    return SimpleNamespace(
        key="automation/luna-companion",
        enabled=True,
        runtime_state=PluginRuntimeState.ACTIVE,
        runtime_instance_id="companion-test",
        manifest=SimpleNamespace(name="Luna Companion", provides=["active"]),
        generation_scope=SimpleNamespace(closed=False),
        data_path=None,
    )


def _storage(tmp_path):
    return PluginStoragePort(plugin=_plugin(tmp_path), root=tmp_path)


def test_activation_accumulates_semantic_signal_and_decays():
    high = _activation(
        continuity=1,
        interest=1,
        novelty=1,
        urgency=1,
        availability=1,
        semantic_value=1,
        fatigue=0,
        interruption_cost=0,
        duplicate_penalty=0,
        previous=0,
        elapsed_seconds=0,
    )
    decayed = _activation(
        continuity=0,
        interest=0,
        novelty=0,
        urgency=0,
        availability=0,
        semantic_value=0,
        fatigue=0,
        interruption_cost=0,
        duplicate_penalty=0,
        previous=high,
        elapsed_seconds=24 * 3600,
    )

    assert high > 0.75
    assert decayed < high


def test_quiet_hours_support_cross_midnight_ranges():
    assert _in_quiet_hours(datetime(2026, 7, 20, 23, 30, tzinfo=UTC), [("23:00", "08:00")])
    assert _in_quiet_hours(datetime(2026, 7, 20, 7, 30, tzinfo=UTC), [("23:00", "08:00")])
    assert not _in_quiet_hours(datetime(2026, 7, 20, 12, 0, tzinfo=UTC), [("23:00", "08:00")])


class _LLM:
    def __init__(self, value):
        self.value = value
        self.calls = []

    async def complete(self, prompt, **kwargs):
        self.calls.append(prompt)
        return SimpleNamespace(content=json.dumps(self.value, ensure_ascii=False))


class _Handle:
    async def outcome(self):
        return SimpleNamespace(succeeded=True)


class _Conversation:
    def __init__(self, status):
        self.status_value = status
        self.intents = []

    async def status(self, session_key):
        return self.status_value

    async def submit_intent(self, intent):
        self.intents.append(intent)
        return _Handle()


@pytest.mark.asyncio
async def test_companion_low_llm_score_keeps_candidate_without_submitting(tmp_path):
    storage = _storage(tmp_path)
    store = CompanionStore(storage)
    llm = _LLM({
        "continuity": 0.1,
        "interest": 0.1,
        "novelty": 0.1,
        "semantic_value": 0.1,
        "confidence": 0.9,
    })
    conversation = _Conversation(SimpleNamespace(
        session_key="wechat:test",
        busy=False,
        last_user_at=(datetime.now(UTC) - timedelta(hours=10)).isoformat(),
        recent_user_messages=("我今天有考试",),
    ))
    ctx = SimpleNamespace(
        runtime=SimpleNamespace(),
        resources=SimpleNamespace(llm=llm, conversation=conversation),
    )
    config = CompanionConfig(
        active={"enabled": True, "sessions": ["wechat:test"]},
        check_in={"enabled": False},
        review_threshold=0.75,
    )
    runner = CompanionRunner(ctx, config, store)

    await runner.cycle()

    assert not conversation.intents
    state = await store.snapshot()
    assert state["candidates"]
    assert next(iter(state["candidates"].values()))["activation"] < 0.75

    # A deferred candidate is not sent back to the model on every wake-up.
    await runner.cycle()
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_companion_high_llm_score_submits_one_intent(tmp_path):
    storage = _storage(tmp_path)
    store = CompanionStore(storage)
    llm = _LLM({
        "continuity": 1,
        "interest": 1,
        "novelty": 1,
        "semantic_value": 1,
        "confidence": 0.9,
    })
    conversation = _Conversation(SimpleNamespace(
        session_key="wechat:test",
        busy=False,
        last_user_at=(datetime.now(UTC) - timedelta(hours=10)).isoformat(),
        recent_user_messages=("我今天有考试",),
    ))
    ctx = SimpleNamespace(
        runtime=SimpleNamespace(),
        resources=SimpleNamespace(llm=llm, conversation=conversation),
    )
    config = CompanionConfig(
        active={"enabled": True, "sessions": ["wechat:test"]},
        check_in={"enabled": False},
        review_threshold=0.75,
    )
    runner = CompanionRunner(ctx, config, store)

    await runner.cycle()

    assert len(conversation.intents) == 1
    assert conversation.intents[0].kind == "follow_up"
    state = await store.snapshot()
    assert len(state["sent"]) == 1
