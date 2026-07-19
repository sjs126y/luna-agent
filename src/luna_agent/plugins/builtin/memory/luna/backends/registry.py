"""Factory registry for one family of Luna backends."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BackendRegistration:
    factory: Callable[..., Any]
    validator: Callable[..., list[str]] | None = None


class BackendRegistry:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._registrations: dict[str, BackendRegistration] = {}

    def register(
        self,
        name: str,
        factory: Callable[..., Any],
        *,
        validator: Callable[..., list[str]] | None = None,
    ) -> None:
        key = _normalize(name)
        if not key:
            raise ValueError(f"{self.kind} backend name must not be empty")
        if key in self._registrations:
            raise ValueError(f"{self.kind} backend already registered: {key}")
        self._registrations[key] = BackendRegistration(factory, validator)

    def create(self, name: str, /, **kwargs: Any) -> Any:
        key = _normalize(name)
        registration = self._registrations.get(key)
        if registration is None:
            available = ", ".join(self.names()) or "none"
            raise ValueError(f"Unknown {self.kind} backend {key!r}; available: {available}")
        return registration.factory(**kwargs)

    def validate(self, name: str, /, **kwargs: Any) -> list[str]:
        key = _normalize(name)
        registration = self._registrations.get(key)
        if registration is None:
            available = ", ".join(self.names()) or "none"
            return [f"unknown {self.kind} backend {key!r}; available: {available}"]
        if registration.validator is None:
            return []
        return list(registration.validator(**kwargs))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._registrations))


def _normalize(name: str) -> str:
    return str(name or "").strip().lower().replace("-", "_")
