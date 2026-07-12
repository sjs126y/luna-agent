"""Lumora external memory provider plugin registration."""

from __future__ import annotations

import importlib.util

from personal_agent.memory.models import ProviderReadiness


def validate_config(*, context, **kwargs) -> ProviderReadiness:
    missing = []
    if context.embedding.api_mode != "openai_compatible":
        missing.append("memory.embedding.api_mode=openai_compatible")
    for label, value in (
        ("memory.embedding.base_url", context.embedding.base_url),
        ("memory.embedding.model", context.embedding.model),
        ("memory.embedding API key", context.embedding.api_key),
        ("memory.qdrant.url", context.qdrant.url),
        ("memory.qdrant.collection", context.qdrant.collection),
    ):
        if not value:
            missing.append(label)
    if importlib.util.find_spec("qdrant_client") is None:
        missing.append("qdrant-client dependency")
    return ProviderReadiness(
        provider="lumora", available=not missing,
        reason=("missing: " + ", ".join(missing)) if missing else "",
    )


def create_provider(*, context, archive, **kwargs):
    from personal_agent.plugins.builtin.memory.lumora.embedding import BailianEmbeddingClient
    from personal_agent.plugins.builtin.memory.lumora.provider import LumoraMemoryProvider
    from personal_agent.plugins.builtin.memory.lumora.qdrant_store import QdrantMemoryIndex

    return LumoraMemoryProvider(
        archive=archive,
        context=context,
        embedding=BailianEmbeddingClient(context.embedding),
        vector_index=QdrantMemoryIndex(context.qdrant, dimensions=context.embedding.dimensions),
    )


def register(ctx) -> None:
    ctx.register_memory_provider(name="lumora", factory=create_provider, validator=validate_config)
