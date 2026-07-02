"""Platform runtime public API."""

from personal_agent.platforms.core import (
    BasePlatformAdapter,
    ChatInfo,
    PlatformEntry,
    PlatformRegistry,
    SendResult,
    platform_registry,
)

__all__ = [
    "BasePlatformAdapter",
    "ChatInfo",
    "PlatformEntry",
    "PlatformRegistry",
    "SendResult",
    "platform_registry",
]
