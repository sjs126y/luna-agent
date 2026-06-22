"""Compressor abstract base — config-driven strategy selection."""

from abc import ABC, abstractmethod


class Compressor(ABC):
    """Abstract compressor. Implementations: SimpleCompressor, ExternalModelCompressor."""

    @abstractmethod
    async def compress(
        self,
        messages: list[dict],
        system_prompt: str,
        transport,
        protect_head: int = 2,
        protect_tail: int = 6,
    ) -> list[dict]:
        """Return compressed message list. Protects head/tail messages."""
        ...
