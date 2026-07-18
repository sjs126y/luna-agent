"""Reverse-order cleanup scope owned by one plugin generation."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


Cleanup = Callable[[], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class CleanupFailure:
    name: str
    error: str


class PluginGenerationScope:
    def __init__(self) -> None:
        self._cleanups: list[tuple[str, Cleanup]] = []
        self._closed = False
        self.failures: list[CleanupFailure] = []

    @property
    def closed(self) -> bool:
        return self._closed

    def defer(self, name: str, cleanup: Cleanup) -> None:
        if self._closed:
            raise RuntimeError("plugin generation scope is closed")
        label = str(name or "").strip()
        if not label:
            raise ValueError("plugin generation cleanup name is required")
        if not callable(cleanup):
            raise TypeError("plugin generation cleanup must be callable")
        self._cleanups.append((label, cleanup))

    async def aclose(self) -> list[CleanupFailure]:
        if self._closed:
            return list(self.failures)
        self._closed = True
        for name, cleanup in reversed(self._cleanups):
            try:
                result = cleanup()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                self.failures.append(
                    CleanupFailure(name=name, error=f"{type(exc).__name__}: {exc}")
                )
        self._cleanups.clear()
        return list(self.failures)
