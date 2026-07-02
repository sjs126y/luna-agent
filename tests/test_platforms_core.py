"""Platform runtime import boundaries."""

from __future__ import annotations


def test_platform_core_is_public_import_path():
    from personal_agent.platforms.core import BasePlatformAdapter, PlatformEntry, platform_registry

    assert BasePlatformAdapter.__name__ == "BasePlatformAdapter"
    assert PlatformEntry.__name__ == "PlatformEntry"
    assert platform_registry is not None


def test_legacy_adapters_base_reexports_platform_core():
    from personal_agent.adapters.base import BasePlatformAdapter as LegacyBasePlatformAdapter
    from personal_agent.adapters.base import PlatformEntry as LegacyPlatformEntry
    from personal_agent.adapters.base import platform_registry as legacy_platform_registry
    from personal_agent.platforms.core import BasePlatformAdapter, PlatformEntry, platform_registry

    assert LegacyBasePlatformAdapter is BasePlatformAdapter
    assert LegacyPlatformEntry is PlatformEntry
    assert legacy_platform_registry is platform_registry
