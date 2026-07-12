from __future__ import annotations

import pytest

from personal_agent.memory.models import ProviderReadiness
from personal_agent.memory.provider_registry import MemoryProviderRegistry


def _validator(**kwargs) -> ProviderReadiness:
    return ProviderReadiness(provider="demo", available=True)


def test_memory_provider_registry_tracks_plugin_ownership() -> None:
    registry = MemoryProviderRegistry()
    factory = lambda **kwargs: object()
    registry.register(name="demo", plugin_key="memory/demo", factory=factory, validator=_validator)

    assert registry.names() == ("demo",)
    assert registry.get("DEMO").factory is factory
    assert registry.unregister_plugin("memory/demo") == ["demo"]
    assert registry.get("demo") is None


def test_memory_provider_registry_rejects_cross_plugin_override() -> None:
    registry = MemoryProviderRegistry()
    registry.register(name="demo", plugin_key="memory/a", factory=object, validator=_validator)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(name="demo", plugin_key="memory/b", factory=object, validator=_validator)
