"""Embedding-backed external memory provider plugin."""


def _create_external_provider(settings=None, data_dir=None, force: bool = False, **kwargs):
    if (
        not force
        and settings is not None
        and getattr(settings, "memory_external_provider", "none") != "embedding"
    ):
        return None

    from personal_agent.plugins.builtin.memory.embedding.provider import (
        EmbeddingMemoryProvider,
        set_external_instance,
    )

    root = data_dir
    if root is None and settings is not None:
        root = settings.agent_data_dir / "memory"
    if root is None:
        return None

    provider = EmbeddingMemoryProvider(root)
    set_external_instance(provider)
    return provider


def register(ctx) -> None:
    ctx.register_hook("create_external_memory_provider", _create_external_provider, priority=10)
