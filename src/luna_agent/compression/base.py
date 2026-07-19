"""ContextEngine — abstract base for compression strategies.
Pluggable like MemoryProvider: config selects engine, factory creates it.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Awaitable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CompactionMetadata:
    """Diagnostics describing one replacement-history checkpoint."""

    trigger: str = "auto"
    pre_tokens: int = 0
    post_tokens: int = 0
    summary_tokens: int = 0
    retained_user_tokens: int = 0
    pre_message_count: int = 0
    post_message_count: int = 0
    model: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CompactionResult:
    """A complete context checkpoint ready to replace old history."""

    replacement_history: list[dict]
    summary: str
    metadata: CompactionMetadata


class ContextEngine(ABC):
    """Abstract compression engine. One instance per Agent, reused across turns.

    Built-in: ContextCompressor (prune + LLM summary).
    Future: LCM plugin, external compression services, etc.
    """

    name: str = "base"

    # Per-session state (set/updated by subclasses)
    last_prompt_tokens: int = 0
    threshold_tokens: int = 0
    context_length: int = 256000

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
        *,
        trigger: str = "auto",
    ) -> CompactionResult:
        """Return a replacement-history checkpoint."""
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
