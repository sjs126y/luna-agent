"""Shared domain models for internal and external memory."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class ObservationKind(str, Enum):
    PREFERENCE = "preference"
    FACT = "fact"
    EVENT = "event"
    RELATIONSHIP = "relationship"
    COMMITMENT = "commitment"
    BEHAVIOR = "behavior"


class MemoryChangeAction(str, Enum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    NONE = "NONE"


class InternalPatchAction(str, Enum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    SKIP = "SKIP"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class MemoryScope:
    user_id: str
    agent_id: str = "luna"
    session_key: str = ""
    profile: str = "default"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class Observation:
    kind: ObservationKind
    content: str
    importance: float = 0.5
    long_term: bool = True
    source_turn_ids: tuple[str, ...] = ()
    id: str = field(default_factory=lambda: uuid4().hex)
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        content = self.content.strip()
        if not content:
            raise ValueError("Observation content must not be empty")
        if not 0 <= self.importance <= 1:
            raise ValueError("Observation importance must be between 0 and 1")
        object.__setattr__(self, "content", content)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        data["source_turn_ids"] = list(self.source_turn_ids)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Observation:
        return cls(
            id=str(data.get("id") or uuid4().hex),
            kind=ObservationKind(str(data["kind"]).lower()),
            content=str(data["content"]),
            importance=float(data.get("importance", 0.5)),
            long_term=bool(data.get("long_term", True)),
            source_turn_ids=tuple(str(item) for item in data.get("source_turn_ids", [])),
            created_at=str(data.get("created_at") or utc_now()),
        )


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    content: str
    kind: ObservationKind = ObservationKind.FACT
    importance: float = 0.5
    score: float = 0.0
    provider: str = ""
    scope: MemoryScope | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        data["scope"] = self.scope.as_dict() if self.scope else None
        return data


@dataclass(frozen=True)
class MemoryChange:
    action: MemoryChangeAction
    observation_id: str
    memory_id: str = ""
    content: str = ""
    previous_content: str = ""
    reason: str = ""
    created_at: str = field(default_factory=utc_now)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["action"] = self.action.value
        return data


@dataclass(frozen=True)
class MemoryReviewResult:
    observations: tuple[Observation, ...] = ()
    changes: tuple[MemoryChange, ...] = ()
    provider: str = ""
    batch_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "observations": [item.as_dict() for item in self.observations],
            "changes": [item.as_dict() for item in self.changes],
            "provider": self.provider,
            "batch_id": self.batch_id,
        }


@dataclass(frozen=True)
class ProviderReadiness:
    provider: str
    available: bool
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InternalMemorySnapshot:
    profile: str
    revision: int
    content: str
    file_hashes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class InternalPatchOperation:
    action: InternalPatchAction
    observation_id: str
    target_file: str
    content: str = ""
    entry_id: str = ""
    reason: str = ""

