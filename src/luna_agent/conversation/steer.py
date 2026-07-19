"""Runtime steer signals for in-flight conversation turns."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal


SteerStatus = Literal["pending", "consumed", "expired"]

MAX_PENDING_PER_SESSION = 10
MAX_STEER_TEXT_CHARS = 2000
RECENT_STEER_LIMIT = 50


@dataclass
class SteerSignal:
    id: str
    session_key: str
    text: str
    source: Any = None
    created_at: float = 0.0
    turn_id: str = ""
    status: SteerStatus = "pending"
    consumed_at: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_key": self.session_key,
            "turn_id": self.turn_id,
            "status": self.status,
            "text_preview": _preview(self.text),
            "created_at": self.created_at,
            "consumed_at": self.consumed_at,
        }


class SteerManager:
    def __init__(
        self,
        *,
        max_pending_per_session: int = MAX_PENDING_PER_SESSION,
        max_text_chars: int = MAX_STEER_TEXT_CHARS,
        recent_limit: int = RECENT_STEER_LIMIT,
    ) -> None:
        self.max_pending_per_session = max(1, int(max_pending_per_session))
        self.max_text_chars = max(1, int(max_text_chars))
        self._pending: dict[str, deque[SteerSignal]] = {}
        self._active_turns: dict[str, str] = {}
        self._recent: deque[SteerSignal] = deque(maxlen=max(1, int(recent_limit)))

    def begin_turn(self, session_key: str, turn_id: str) -> None:
        session = _clean_key(session_key)
        turn = _clean_key(turn_id)
        if not session or not turn:
            return
        self._active_turns[session] = turn
        for signal in self._pending.get(session, ()):
            if signal.status == "pending" and not signal.turn_id:
                signal.turn_id = turn

    def add(self, session_key: str, source: Any, text: str) -> SteerSignal:
        session = _clean_key(session_key)
        cleaned = _clean_text(text, limit=self.max_text_chars)
        signal = SteerSignal(
            id=f"st_{uuid.uuid4().hex[:10]}",
            session_key=session,
            turn_id=self._active_turns.get(session, ""),
            text=cleaned,
            source=source,
            created_at=time.time(),
        )
        queue = self._pending.setdefault(session, deque())
        while len(queue) >= self.max_pending_per_session:
            expired = queue.popleft()
            if expired.status == "pending":
                expired.status = "expired"
        queue.append(signal)
        self._recent.append(signal)
        return signal

    def consume(self, session_key: str, turn_id: str, *, limit: int = MAX_PENDING_PER_SESSION) -> list[SteerSignal]:
        session = _clean_key(session_key)
        turn = _clean_key(turn_id)
        if not session or not turn:
            return []
        queue = self._pending.get(session)
        if not queue:
            return []
        consumed: list[SteerSignal] = []
        remaining: deque[SteerSignal] = deque()
        max_count = max(1, int(limit))
        while queue:
            signal = queue.popleft()
            if signal.status != "pending":
                continue
            if signal.turn_id == turn and len(consumed) < max_count:
                signal.status = "consumed"
                signal.consumed_at = time.time()
                consumed.append(signal)
                continue
            remaining.append(signal)
        if remaining:
            self._pending[session] = remaining
        else:
            self._pending.pop(session, None)
        return consumed

    def end_turn(self, session_key: str, turn_id: str) -> list[SteerSignal]:
        session = _clean_key(session_key)
        turn = _clean_key(turn_id)
        expired: list[SteerSignal] = []
        if not session or not turn:
            return expired
        if self._active_turns.get(session) == turn:
            self._active_turns.pop(session, None)
        queue = self._pending.get(session)
        if not queue:
            return expired
        remaining: deque[SteerSignal] = deque()
        while queue:
            signal = queue.popleft()
            if signal.status == "pending" and signal.turn_id in {"", turn}:
                signal.status = "expired"
                expired.append(signal)
                continue
            remaining.append(signal)
        if remaining:
            self._pending[session] = remaining
        else:
            self._pending.pop(session, None)
        return expired

    def turn_summary(self, session_key: str, turn_id: str) -> dict[str, Any]:
        session = _clean_key(session_key)
        turn = _clean_key(turn_id)
        items = [
            signal.snapshot()
            for signal in self._recent
            if signal.session_key == session and signal.turn_id == turn
        ]
        return steer_items_summary(items)

    def snapshot(self, session_key: str | None = None) -> dict[str, Any]:
        if session_key is not None:
            session = _clean_key(session_key)
            pending = [
                signal.snapshot()
                for signal in self._pending.get(session, ())
                if signal.status == "pending"
            ]
            recent = [
                signal.snapshot()
                for signal in self._recent
                if signal.session_key == session
            ]
            return {
                "session_key": session,
                "active_turn_id": self._active_turns.get(session, ""),
                "pending_count": len(pending),
                "pending_items": pending,
                "recent_items": recent,
            }

        pending_count = sum(
            1
            for queue in self._pending.values()
            for signal in queue
            if signal.status == "pending"
        )
        sessions = sorted(set(self._active_turns) | set(self._pending))
        return {
            "pending_steer_count": pending_count,
            "active_steer_sessions": sessions,
            "recent_steers": [signal.snapshot() for signal in self._recent],
        }


@dataclass(frozen=True, slots=True)
class ActiveTurn:
    session_key: str
    turn_id: str
    request_id: str
    started_at: float


class ActiveTurnRegistry:
    """Authoritative active-turn state owned by the conversation coordinator."""

    def __init__(self, *, steer: SteerManager | None = None) -> None:
        self._steer = steer or SteerManager()
        self._turns: dict[str, ActiveTurn] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def begin_turn(
        self,
        session_key: str,
        turn_id: str,
        *,
        request_id: str = "",
        task: asyncio.Task | None = None,
    ) -> ActiveTurn:
        session = _clean_key(session_key)
        turn = _clean_key(turn_id)
        if not session or not turn:
            raise ValueError("session_key and turn_id are required")
        if session in self._turns:
            raise RuntimeError(f"session already has an active turn: {session}")
        active = ActiveTurn(
            session_key=session,
            turn_id=turn,
            request_id=_clean_key(request_id),
            started_at=time.time(),
        )
        self._turns[session] = active
        if task is not None:
            self._tasks[session] = task
        self._steer.begin_turn(session, turn)
        return active

    def end_turn(self, session_key: str, turn_id: str) -> list[SteerSignal]:
        session = _clean_key(session_key)
        turn = _clean_key(turn_id)
        current = self._turns.get(session)
        if current is not None and current.turn_id == turn:
            self._turns.pop(session, None)
            self._tasks.pop(session, None)
        return self._steer.end_turn(session, turn)

    def active_turn(self, session_key: str) -> ActiveTurn | None:
        return self._turns.get(_clean_key(session_key))

    def add(self, session_key: str, source: Any, text: str) -> SteerSignal:
        session = _clean_key(session_key)
        if session not in self._turns:
            raise RuntimeError("session has no active turn")
        return self._steer.add(session, source, text)

    def consume(self, session_key: str, turn_id: str, *, limit: int = MAX_PENDING_PER_SESSION) -> list[SteerSignal]:
        return self._steer.consume(session_key, turn_id, limit=limit)

    def turn_summary(self, session_key: str, turn_id: str) -> dict[str, Any]:
        return self._steer.turn_summary(session_key, turn_id)

    def snapshot(self, session_key: str | None = None) -> dict[str, Any]:
        snapshot = self._steer.snapshot(session_key)
        if session_key is not None:
            active = self.active_turn(session_key)
            snapshot["active_request_id"] = active.request_id if active else ""
            return snapshot
        snapshot["active_turn_count"] = len(self._turns)
        snapshot["active_turns"] = [
            {
                "session_key": item.session_key,
                "turn_id": item.turn_id,
                "request_id": item.request_id,
                "started_at": item.started_at,
            }
            for item in self._turns.values()
        ]
        return snapshot

    def cancel(self, session_key: str) -> bool:
        task = self._tasks.get(_clean_key(session_key))
        if task is None or task.done():
            return False
        task.cancel()
        return True


def steer_items_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    received = len(items)
    consumed = sum(1 for item in items if item.get("status") == "consumed")
    expired = sum(1 for item in items if item.get("status") == "expired")
    pending = sum(1 for item in items if item.get("status") == "pending")
    return {
        "received": received,
        "consumed": consumed,
        "expired": expired,
        "pending": pending,
        "items": items,
    }


def _clean_key(value: str) -> str:
    return str(value or "").strip()


def _clean_text(value: str, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def _preview(value: str, limit: int = 200) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
