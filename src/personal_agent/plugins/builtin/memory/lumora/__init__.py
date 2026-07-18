"""Lumora external memory provider plugin registration."""

from __future__ import annotations

from personal_agent.memory.models import ProviderReadiness


def validate_config(*, context, **kwargs) -> ProviderReadiness:
    from personal_agent.plugins.builtin.memory.lumora.backends.factory import validate_lumora_backends

    missing = validate_lumora_backends(context)
    return ProviderReadiness(
        provider="lumora", available=not missing,
        reason=("missing: " + ", ".join(missing)) if missing else "",
    )


def create_provider(*, context, archive, **kwargs):
    from personal_agent.plugins.builtin.memory.lumora.backends.factory import build_lumora_backends
    from personal_agent.plugins.builtin.memory.lumora.provider import LumoraMemoryProvider

    backends = build_lumora_backends(context=context, archive=archive)

    return LumoraMemoryProvider(
        archive=archive,
        context=context,
        embedding=backends.embedding,
        vector_index=backends.vector,
        keyword_index=backends.keyword,
        fusion=backends.fusion,
        reranker=backends.reranker,
        retrieval_config=backends.config.retrieval,
    )


def register(ctx) -> None:
    ctx.register.memory_provider(name="lumora", factory=create_provider, validator=validate_config)
