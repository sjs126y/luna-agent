"""Mem0 provider plugin registration."""

from __future__ import annotations

import importlib.util

from luna_agent.memory.models import ProviderReadiness


def validate_config(*, context, **kwargs) -> ProviderReadiness:
    available = importlib.util.find_spec("mem0") is not None
    return ProviderReadiness(
        provider="mem0", available=available,
        reason="mem0ai dependency is not installed" if not available else "",
    )


def create_provider(*, context, archive, **kwargs):
    from luna_agent.plugins.builtin.memory.mem0.provider import Mem0MemoryProvider

    return Mem0MemoryProvider(context=context, archive=archive)


def register(ctx) -> None:
    ctx.register.memory_provider(name="mem0", factory=create_provider, validator=validate_config)
