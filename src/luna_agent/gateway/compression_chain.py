"""Compression chain — tracks session_id lineage after compression.

When a session is compressed, a new session_id is created to hold the
compressed messages. The old session retains its full history for audit.
The chain maps old → new so get_current_session_id() can walk to the latest.
"""

from __future__ import annotations

import logging
from pathlib import Path

from luna_agent.persistence.json_store import read_json_object, write_json_atomic

logger = logging.getLogger(__name__)


class CompressionChain:
    """Persisted chain of old_session_id → new_session_id mappings."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._chain: dict[str, str] = {}

    def load(self) -> None:
        if not self._path.exists():
            return
        data = read_json_object(self._path, {})
        self._chain = {
            str(old): str(new)
            for old, new in data.items()
            if isinstance(old, str) and isinstance(new, str)
        }
        logger.info("Loaded compression chain: %d entries", len(self._chain))

    def save(self) -> None:
        write_json_atomic(self._path, self._chain)

    def link(self, old_session_id: str, new_session_id: str) -> None:
        """Record that old_session was compressed into new_session."""
        self._chain[old_session_id] = new_session_id
        self.save()
        logger.info("Compression chain: %s → %s", old_session_id[:8], new_session_id[:8])

    def resolve(self, session_id: str) -> str:
        """Walk the chain to find the latest session_id."""
        seen = {session_id}
        current = session_id
        while current in self._chain:
            current = self._chain[current]
            if current in seen:
                logger.error("Cycle detected in compression chain at %s", current[:8])
                return session_id
            seen.add(current)
        return current

    def get_chain(self, session_id: str) -> list[str]:
        """Return full lineage from session_id to latest (including all intermediate)."""
        result = [session_id]
        seen = {session_id}
        current = session_id
        while current in self._chain:
            current = self._chain[current]
            if current in seen:
                break
            seen.add(current)
            result.append(current)
        return result

    def get_descendants(self, session_id: str) -> list[str]:
        """Return compressed descendants for a session, excluding the input id."""
        return self.get_chain(session_id)[1:]

    def remove_chain(self, session_ids: list[str] | set[str]) -> None:
        """Remove mappings that point from or to any provided session id."""
        targets = set(session_ids)
        if not targets:
            return
        changed = False
        for old_id, new_id in list(self._chain.items()):
            if old_id in targets or new_id in targets:
                del self._chain[old_id]
                changed = True
        if changed:
            self.save()
