"""Luna external memory provider plugin registration."""

from __future__ import annotations

from luna_agent.memory.models import ProviderReadiness


def validate_config(*, context, **kwargs) -> ProviderReadiness:
    from luna_agent.plugins.builtin.memory.luna.backends.factory import validate_luna_backends

    missing = validate_luna_backends(context)
    return ProviderReadiness(
        provider="luna", available=not missing,
        reason=("missing: " + ", ".join(missing)) if missing else "",
    )


def create_provider(*, context, archive, **kwargs):
    from luna_agent.plugins.builtin.memory.luna.backends.factory import build_luna_backends
    from luna_agent.plugins.builtin.memory.luna.provider import LunaMemoryProvider

    backends = build_luna_backends(context=context, archive=archive)

    return LunaMemoryProvider(
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
    ctx.register.memory_provider(name="luna", factory=create_provider, validator=validate_config)
