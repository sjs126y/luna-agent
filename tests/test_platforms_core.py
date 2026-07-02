"""Platform runtime import boundaries."""

from __future__ import annotations


def test_platform_core_is_public_import_path():
    from personal_agent.platforms.core import BasePlatformAdapter, PlatformEntry, platform_registry

    assert BasePlatformAdapter.__name__ == "BasePlatformAdapter"
    assert PlatformEntry.__name__ == "PlatformEntry"
    assert platform_registry is not None
