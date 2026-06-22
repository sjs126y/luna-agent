"""ToolEntry — what gets registered."""

from dataclasses import dataclass, field
from collections.abc import Callable, Awaitable


@dataclass
class ToolEntry:
    name: str
    description: str
    schema: dict                # OpenAI/Anthropic function schema
    handler: Callable[..., Awaitable[str]]
    toolset: str = "general"    # "web" | "terminal" | "memory" | ...
    check_fn: Callable[[], bool] | None = None  # dependency check → True/False
    is_parallel_safe: bool = True
    is_destructive: bool = False
