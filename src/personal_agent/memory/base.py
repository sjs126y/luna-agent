"""MemoryProvider abstract base for plugin-backed implementations."""

from abc import ABC, abstractmethod
from typing import Any


class MemoryProvider(ABC):
    """Base interface implemented by memory provider plugins."""

    @abstractmethod
    async def prefetch(self, user_message: str) -> list[dict]:
        """Return message fragments to inject in api_messages. NOT persisted."""
        ...

    @abstractmethod
    async def save(self, content: str) -> None:
        """Persist a new memory entry."""
        ...

    @abstractmethod
    async def search(self, query: str) -> list[str]:
        """Search memory entries."""
        ...

    @abstractmethod
    async def load_all(self) -> list[str]:
        """Return all memory entries for system prompt injection."""
        ...

    @abstractmethod
    def get_system_prompt_text(self) -> str:
        """Return formatted text for system prompt."""
        ...

    async def delete(self, identifier: str, *, target: str = "") -> bool:
        """Optionally delete a memory entry by provider-specific id or index."""
        return False

    async def list_entries(self, *, target: str = "all") -> list[dict[str, Any]]:
        """Optionally return structured memory entries."""
        return [
            {"id": str(index), "index": index, "text": text}
            for index, text in enumerate(await self.load_all(), start=1)
        ]

    async def search_entries(self, query: str, *, target: str = "all") -> list[dict[str, Any]]:
        """Optionally return structured search results."""
        return [
            {"id": str(index), "index": index, "text": text}
            for index, text in enumerate(await self.search(query), start=1)
        ]

    def health_snapshot(self) -> dict[str, Any]:
        """Return provider-specific health data without performing expensive work."""
        return {
            "provider": type(self).__name__,
            "available": True,
            "last_error": "",
        }
