from types import SimpleNamespace

import pytest

from luna_agent.memory.models import MemoryScope
from luna_agent.plugins.builtin.memory.luna.backends import BackendRegistry, SearchHit
from luna_agent.plugins.builtin.memory.luna.backends.config import LunaBackendConfig


def test_backend_registry_creates_provider_without_qdrant_specific_arguments() -> None:
    registry = BackendRegistry("vector")
    registry.register("pgvector", lambda *, options: SimpleNamespace(options=options))

    backend = registry.create("pgvector", options={"dsn": "postgresql://memory"})

    assert backend.options == {"dsn": "postgresql://memory"}
    assert registry.names() == ("pgvector",)


def test_backend_registry_rejects_duplicate_and_unknown_providers() -> None:
    registry = BackendRegistry("embedding")
    registry.register("openai-compatible", lambda: object())

    with pytest.raises(ValueError, match="already registered"):
        registry.register("openai_compatible", lambda: object())
    with pytest.raises(ValueError, match="Unknown embedding backend"):
        registry.create("local")


def test_search_hit_uses_scope_neutral_backend_contract() -> None:
    scope = MemoryScope(user_id="user", profile="default")
    hit = SearchHit(memory_id="memory", source="pgvector", rank=1, score=0.9)

    assert scope.user_id == "user"
    assert hit == SearchHit("memory", "pgvector", 1, 0.9)


def test_luna_backend_config_uses_provider_specific_options() -> None:
    config = LunaBackendConfig.from_options({
        "vector": {"provider": "pgvector", "dsn_env": "MEMORY_POSTGRES_DSN"},
        "reranker": {"provider": "cross_encoder", "model": "bge-reranker"},
    })

    assert config.vector.provider == "pgvector"
    assert config.vector.options == {"dsn_env": "MEMORY_POSTGRES_DSN"}
    assert config.embedding.provider == "openai_compatible"
    assert config.keyword.provider == "sqlite_fts5"
    assert config.reranker.provider == "cross_encoder"
