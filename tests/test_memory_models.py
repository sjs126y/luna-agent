from __future__ import annotations

import pytest

from personal_agent.memory.models import MemoryScope, Observation, ObservationKind


def test_observation_round_trips() -> None:
    observation = Observation(
        kind=ObservationKind.PREFERENCE,
        content="  prefers concise answers  ",
        importance=0.8,
        source_turn_ids=("turn-1",),
    )

    restored = Observation.from_dict(observation.as_dict())

    assert restored == observation
    assert restored.content == "prefers concise answers"


def test_observation_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        Observation(kind=ObservationKind.FACT, content=" ")
    with pytest.raises(ValueError, match="between 0 and 1"):
        Observation(kind=ObservationKind.FACT, content="fact", importance=2)


def test_memory_scope_serializes() -> None:
    scope = MemoryScope(user_id="u1", session_key="cli:1", profile="work")
    assert scope.as_dict() == {
        "user_id": "u1",
        "agent_id": "lumora",
        "session_key": "cli:1",
        "profile": "work",
    }
