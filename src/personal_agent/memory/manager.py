"""MemoryManager — shell that wires builtin + optional external MemoryProvider."""

from __future__ import annotations

from personal_agent.memory.base import MemoryProvider


class MemoryManager:
    def __init__(
        self,
        builtin: MemoryProvider,
        external: MemoryProvider | None = None,
    ) -> None:
        self._builtin = builtin
        self._external = external

    # ── system prompt ─────────────────────────────────

    def get_system_prompt_text(self) -> str:
        """Memory text injected into system prompt (builtin only)."""
        return self._builtin.get_system_prompt_text()

    # ── prefetch (external only, injects to api_messages) ──

    async def prefetch(self, user_message: str) -> list[dict]:
        """External memory search results for api_messages injection."""
        if self._external:
            return await self._external.prefetch(user_message)
        return []

    # ── save ──────────────────────────────────────────

    async def save(self, content: str) -> None:
        await self._builtin.save(content)
        if self._external:
            await self._external.save(content)

    @property
    def builtin(self) -> MemoryProvider:
        return self._builtin

    @property
    def external(self) -> MemoryProvider | None:
        return self._external
