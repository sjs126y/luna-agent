"""Legacy adapter package.

Platform adapters are registered by platform plugins. New code should import
platform runtime types from :mod:`personal_agent.platforms`.
"""

from personal_agent.platforms import (
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
