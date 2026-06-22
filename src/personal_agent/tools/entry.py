"""ToolEntry — what gets registered."""

from dataclasses import dataclass, field
from collections.abc import Callable, Awaitable


@dataclass
class ToolEntry:
    name: str
    description: str
    parameters: dict          # JSON Schema
    handler: Callable[..., Awaitable[str]]
    toolset: str = "general"
    is_parallel_safe: bool = True
    is_destructive: bool = False
