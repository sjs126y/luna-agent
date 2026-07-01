"""ToolEntry — what gets registered."""

from dataclasses import dataclass, field
from collections.abc import Callable, Awaitable
from typing import Any


@dataclass
class ToolEntry:
    name: str
    description: str
    schema: dict                # OpenAI/Anthropic function schema
    handler: Callable[..., Awaitable[str]]
    toolset: str = "general"    # "web" | "terminal" | "memory" | ...
    check_fn: Callable[[], bool] | None = None  # dependency check → True/False
    precheck: Callable[[dict[str, Any]], str | None] | None = None
    is_parallel_safe: bool = True
    is_destructive: bool = False
