"""ToolRegistry — module-level singleton. Tools self-register on import."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from personal_agent.tools.entry import ToolEntry

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, ToolEntry] = {}
        self._generation: int = 0

    # ── registration ──────────────────────────────────

    def register(self, entry: ToolEntry) -> None:
        self._entries[entry.name] = entry
        self._generation += 1
        logger.debug("Tool registered: %s (gen=%d)", entry.name, self._generation)

    def unregister(self, name: str) -> None:
        if name in self._entries:
            del self._entries[name]
            self._generation += 1

    def get(self, name: str) -> ToolEntry | None:
        return self._entries.get(name)

    # ── definitions (for LLM system prompt / tool schema) ──

    def get_definitions(self, names: list[str] | None = None) -> list[dict]:
        """Return Anthropic-format tool schemas, sorted by name (deterministic)."""
        result = []
        targets = (
            [self._entries[n] for n in names if n in self._entries]
            if names is not None
            else list(self._entries.values())
        )
        # Sort by name for deterministic byte stream → cache hits
        for entry in sorted(targets, key=lambda e: e.name):
            result.append({
                "name": entry.name,
                "description": entry.description,
                "input_schema": entry.parameters,
            })
        return result

    # ── dispatch ───────────────────────────────────────

    async def dispatch(self, name: str, args: dict) -> str:
        """Look up tool handler and call it."""
        entry = self._entries.get(name)
        if entry is None:
            return f"Error: unknown tool '{name}'"
        try:
            return await entry.handler(**args)
        except Exception as exc:
            logger.exception("Tool '%s' failed", name)
            return f"Error: {exc}"

    @property
    def generation(self) -> int:
        return self._generation


tool_registry = ToolRegistry()
