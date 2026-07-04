"""Platform runtime public API."""

from personal_agent.platforms.core import (
    BasePlatformAdapter,
    ChatInfo,
    PlatformCapabilities,
    PlatformEntry,
    PlatformRegistry,
    SendResult,
    platform_registry,
)

__all__ = [
    "BasePlatformAdapter",
    "ChatInfo",
    "PlatformCapabilities",
    "PlatformEntry",
    "PlatformRegistry",
    "SendResult",
    "platform_registry",
]
