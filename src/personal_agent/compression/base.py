"""ContextEngine — abstract base for compression strategies.
Pluggable like MemoryProvider: config selects engine, factory creates it.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Awaitable
from typing import Any


class ContextEngine(ABC):
    """Abstract compression engine. One instance per Agent, reused across turns.

    Built-in: ContextCompressor (prune + LLM summary).
    Future: LCM plugin, external compression services, etc.
    """

    name: str = "base"

    # Per-session state (set/updated by subclasses)
    last_prompt_tokens: int = 0
    threshold_tokens: int = 0
    context_length: int = 64000

    @abstractmethod
    def should_compress(self, token_count: int, messages: list[dict]) -> bool:
        """Called before every LLM call. Returns True if compression needed."""
        ...

    @abstractmethod
    async def compress(
        self,
        messages: list[dict],
        system_prompt: str,
        transport: Any,
        protect_head: int = 2,
        protect_tail: int = 6,
    ) -> list[dict]:
        """Return compressed message list. Called by build_turn_context."""
        ...

    def on_session_start(self) -> None:
        """Reset per-session compression state."""
        pass

    def on_session_end(self) -> None:
        """Clean up after session ends."""
        pass

    def update_from_response(self, response: Any) -> None:
        """Update token tracking from actual LLM response (runtime trigger)."""
        if hasattr(response, "usage"):
            self.last_prompt_tokens = response.usage.get("input_tokens", 0)
