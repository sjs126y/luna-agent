from __future__ import annotations

from dataclasses import dataclass, field

from personal_agent.artifacts.models import StoredArtifactRef


@dataclass(slots=True)
class TurnResponseDraft:
    session_key: str
    turn_id: str
    selected: list[StoredArtifactRef] = field(default_factory=list)

    async def attach(self, store, artifact_ids: list[str]) -> list[StoredArtifactRef]:
        existing = {item.artifact_id for item in self.selected}
        for artifact_id in artifact_ids:
            value = str(artifact_id or "").strip()
            if not value or value in existing:
                continue
            ref = await store.select(
                value,
                session_key=self.session_key,
                turn_id=self.turn_id,
            )
            self.selected.append(ref)
            existing.add(ref.artifact_id)
        return list(self.selected)

    def safe_summaries(self) -> list[dict]:
        return [item.safe_summary() for item in self.selected]
