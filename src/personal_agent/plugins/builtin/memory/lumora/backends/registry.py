"""Factory registry for one family of Lumora backends."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class BackendRegistry:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._factories: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, factory: Callable[..., Any]) -> None:
        key = _normalize(name)
        if not key:
            raise ValueError(f"{self.kind} backend name must not be empty")
        if key in self._factories:
            raise ValueError(f"{self.kind} backend already registered: {key}")
        self._factories[key] = factory

    def create(self, name: str, /, **kwargs: Any) -> Any:
        key = _normalize(name)
        factory = self._factories.get(key)
        if factory is None:
            available = ", ".join(self.names()) or "none"
            raise ValueError(f"Unknown {self.kind} backend {key!r}; available: {available}")
        return factory(**kwargs)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))


def _normalize(name: str) -> str:
    return str(name or "").strip().lower().replace("-", "_")
