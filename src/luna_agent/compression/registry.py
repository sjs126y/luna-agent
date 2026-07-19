"""Compression registry — maps engine names to factory callables."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

CompressionFactory = Callable[[Any, Any, str], Any]


class CompressionRegistry:
    """Registry of compression engine factories."""

    def __init__(self) -> None:
        self._factories: dict[str, CompressionFactory] = {}
        self._aliases: dict[str, str] = {}

    def register(
        self,
        name: str,
        factory: CompressionFactory,
        *,
        aliases: tuple[str, ...] = (),
    ) -> None:
        canonical = name.strip().lower()
        self._factories[canonical] = factory
        for alias in aliases:
            self._aliases[alias.strip().lower()] = canonical

    def get(self, name: str) -> CompressionFactory:
        key = name.strip().lower()
        key = self._aliases.get(key, key)
        if key not in self._factories:
            raise KeyError(
                f"Unknown compression engine: {name}. Registered: {self.list_engines()}"
            )
        return self._factories[key]

    def list_engines(self) -> list[str]:
        return list(self._factories.keys())

    def resolve(self, name: str) -> str:
        key = name.strip().lower()
        return self._aliases.get(key, key)


compression_registry = CompressionRegistry()

# Import built-in implementations so direct registry imports are populated.
import luna_agent.compression.simple  # noqa: F401
