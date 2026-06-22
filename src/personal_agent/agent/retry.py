"""Retry counters and logic for Agent loop defects.

Per-turn: reset in build_turn_context().
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RetryState:
    empty_content_retries: int = 0        # LLM returned no text, no tool_calls
    invalid_tool_retries: int = 0         # LLM returned malformed tool_calls
    post_tool_empty_retried: bool = False  # After tools ran, LLM returned empty

    MAX_EMPTY_CONTENT = 2
    MAX_INVALID_TOOL = 2
    MAX_POST_TOOL_EMPTY = 1

    def reset(self) -> None:
        self.empty_content_retries = 0
        self.invalid_tool_retries = 0
        self.post_tool_empty_retried = False
