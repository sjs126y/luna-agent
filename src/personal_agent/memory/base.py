"""MemoryProvider abstract base — config-driven implementation."""

from abc import ABC, abstractmethod


class MemoryProvider(ABC):
    """Implementations: FileMemoryProvider, RAGMemoryProvider (future)."""

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
