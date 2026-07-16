"""Immutable per-turn views of mutable session policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from personal_agent.security.models import SecurityContext


@dataclass(frozen=True, slots=True)
class TurnPolicySnapshot:
    session_key: str
    revision: int
    security: SecurityContext
    captured_at: datetime

    @classmethod
    def capture(
        cls,
        session_key: str,
        *,
        revision: int,
        security: SecurityContext,
    ) -> "TurnPolicySnapshot":
        return cls(
            session_key=session_key,
            revision=revision,
            security=security,
            captured_at=datetime.now(UTC),
        )
