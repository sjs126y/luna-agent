"""Runtime state helpers for Gateway platform and agent activity."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any


BACKOFF_DELAYS_SECONDS = (1, 2, 5, 10, 30, 60)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _delay_for_attempt(attempts: int) -> int:
    index = max(0, min(attempts - 1, len(BACKOFF_DELAYS_SECONDS) - 1))
    return BACKOFF_DELAYS_SECONDS[index]


@dataclass
class PlatformRuntime:
    name: str
    adapter: Any | None = None
    backoff_delays_seconds: tuple[int, ...] = BACKOFF_DELAYS_SECONDS
    status: str = "skipped"
    attempts: int = 0
    last_attempt_at: str = ""
    last_connected_at: str = ""
    last_disconnected_at: str = ""
    last_error: str = ""
    next_retry_at: str = ""
    skipped_reason: str = ""
    reconnect_task: asyncio.Task | None = None

    def mark_skipped(self, reason: str) -> None:
        self.status = "skipped"
        self.skipped_reason = reason
        self.last_error = ""
        self.next_retry_at = ""

    def mark_connecting(self) -> None:
        self.status = "connecting" if self.attempts == 0 else "reconnecting"
        self.attempts += 1
        self.last_attempt_at = now_iso()
        self.next_retry_at = ""

    def mark_connected(self, adapter: Any) -> None:
        self.adapter = adapter
        self.status = "connected"
        self.last_connected_at = now_iso()
        self.last_error = ""
        self.next_retry_at = ""
        self.skipped_reason = ""

    def mark_error(self, error: str, adapter: Any | None = None) -> None:
        if adapter is not None:
            self.adapter = adapter
        self.status = "failed"
        self.last_error = error
        self.last_disconnected_at = now_iso()

    def mark_reconnecting(self, delay_seconds: int | None = None) -> None:
        self.status = "reconnecting"
        if delay_seconds is not None:
            self.next_retry_at = time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.localtime(time.time() + delay_seconds),
            )

    def mark_stopped(self) -> None:
        self.status = "stopped"
        self.last_disconnected_at = now_iso()
        self.next_retry_at = ""

    def next_retry_delay(self) -> int:
        if not self.backoff_delays_seconds:
            return _delay_for_attempt(self.attempts)
        index = max(0, min(self.attempts - 1, len(self.backoff_delays_seconds) - 1))
        return self.backoff_delays_seconds[index]

    def snapshot(self) -> dict[str, Any]:
        adapter_health = (
            self.adapter.health_snapshot()
            if self.adapter is not None and hasattr(self.adapter, "health_snapshot")
            else {}
        )
        connected = (
            bool(adapter_health.get("connected", False))
            if adapter_health
            else self.status == "connected"
        )
        last_connect_error = adapter_health.get("last_connect_error") or self.last_error
        data = {
            "name": self.name,
            "adapter": adapter_health.get("adapter") or type(self.adapter).__name__ if self.adapter else "",
            "status": self.status,
            "available": self.status != "skipped",
            "connected": connected,
            "attempts": self.attempts,
            "last_attempt_at": self.last_attempt_at,
            "last_connected_at": adapter_health.get("last_connected_at") or self.last_connected_at,
            "last_disconnected_at": (
                adapter_health.get("last_disconnected_at") or self.last_disconnected_at
            ),
            "last_error": self.last_error,
            "last_connect_error": last_connect_error,
            "last_send_error": adapter_health.get("last_send_error", ""),
            "next_retry_at": self.next_retry_at,
            "skipped_reason": self.skipped_reason,
            "active_sessions": int(adapter_health.get("active_sessions", 0)),
            "pending_messages": int(adapter_health.get("pending_messages", 0)),
            "pending_session_count": int(adapter_health.get("pending_session_count", 0)),
            "capabilities": adapter_health.get("capabilities", {}),
            "adapter_health": adapter_health,
            "send_stats": adapter_health.get("send_stats", {}),
        }
        if self.status == "skipped":
            data["last_connect_error"] = ""
        return data
