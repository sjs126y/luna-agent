"""Platform runtime public API."""

from luna_agent.platforms.core import (
    AttachmentDownloadError,
    BasePlatformAdapter,
    ChatInfo,
    PlatformCapabilities,
    PlatformEntry,
    PlatformRegistry,
    PlatformSetupContext,
    PlatformSetupResult,
    SendResult,
    platform_registry,
)

__all__ = [
    "AttachmentDownloadError",
    "BasePlatformAdapter",
    "ChatInfo",
    "PlatformCapabilities",
    "PlatformEntry",
    "PlatformRegistry",
    "PlatformSetupContext",
    "PlatformSetupResult",
    "SendResult",
    "platform_registry",
]
