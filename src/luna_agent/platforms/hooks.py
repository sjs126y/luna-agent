"""Private adapter callback lists for transport-specific parsing lifecycle."""

from collections.abc import Awaitable, Callable
from typing import Any

AdapterHook = Callable[..., Awaitable[Any]]


class AdapterHooks:
    """Internal adapter callbacks; these are not plugin runtime hooks."""

    def __init__(self) -> None:
        self.on_connect: list[AdapterHook] = []
        self.on_disconnect: list[AdapterHook] = []
        self.on_before_parse: list[AdapterHook] = []
        self.on_after_parse: list[AdapterHook] = []

    async def fire(self, name: str, *args: Any, **kwargs: Any) -> Any:
        callbacks = getattr(self, name, [])
        if not callbacks:
            return args[0] if args else None
        result = None
        for callback in callbacks:
            value = await callback(*args, **kwargs)
            if value is not None:
                result = value
        return result
