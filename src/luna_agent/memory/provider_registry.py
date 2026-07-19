"""Registry for plugin-provided external memory implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from luna_agent.memory.models import ProviderReadiness

MemoryProviderFactory = Callable[..., Any]
MemoryProviderValidator = Callable[..., ProviderReadiness]


@dataclass(frozen=True)
class MemoryProviderRegistration:
    name: str
    plugin_key: str
    factory: MemoryProviderFactory
    validator: MemoryProviderValidator


class MemoryProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, MemoryProviderRegistration] = {}

    def register(
        self,
        *,
        name: str,
        plugin_key: str,
        factory: MemoryProviderFactory,
        validator: MemoryProviderValidator,
    ) -> None:
        key = _normalize_name(name)
        existing = self._providers.get(key)
        if existing is not None and existing.plugin_key != plugin_key:
            raise ValueError(
                f"Memory provider '{key}' is already registered by plugin '{existing.plugin_key}'"
            )
        self._providers[key] = MemoryProviderRegistration(
            name=key,
            plugin_key=plugin_key,
            factory=factory,
            validator=validator,
        )

    def unregister(self, name: str, *, plugin_key: str = "") -> bool:
        key = _normalize_name(name)
        existing = self._providers.get(key)
        if existing is None or (plugin_key and existing.plugin_key != plugin_key):
            return False
        del self._providers[key]
        return True

    def unregister_plugin(self, plugin_key: str) -> list[str]:
        names = [name for name, item in self._providers.items() if item.plugin_key == plugin_key]
        for name in names:
            del self._providers[name]
        return sorted(names)

    def get(self, name: str) -> MemoryProviderRegistration | None:
        return self._providers.get(_normalize_name(name))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def clear(self) -> None:
        self._providers.clear()


def _normalize_name(name: str) -> str:
    value = str(name or "").strip().lower()
    if not value or not value.replace("-", "_").isalnum():
        raise ValueError(f"Invalid memory provider name: {name!r}")
    return value


memory_provider_registry = MemoryProviderRegistry()
