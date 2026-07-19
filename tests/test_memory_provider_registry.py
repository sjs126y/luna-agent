from __future__ import annotations

import pytest

from luna_agent.memory.models import ProviderReadiness
from luna_agent.memory.provider_registry import MemoryProviderRegistry
from luna_agent.memory.provider_registry import memory_provider_registry
from luna_agent.config import Settings
from luna_agent.plugins.core.manager import PluginManager


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


def test_builtin_external_memory_plugins_register_and_unload(tmp_path) -> None:
    memory_provider_registry.clear()
    settings = Settings(agent_data_dir=tmp_path / "data", plugins_dirs=[])
    manager = PluginManager(settings, plugin_dirs=[], state_path=tmp_path / "state.json")
    manager.discover()

    manager.load_plugin("memory/luna")
    manager.load_plugin("memory/mem0")

    assert set(memory_provider_registry.names()) == {"luna", "mem0"}
    manager.disable_plugin("memory/luna")
    assert memory_provider_registry.names() == ("mem0",)
    memory_provider_registry.clear()
