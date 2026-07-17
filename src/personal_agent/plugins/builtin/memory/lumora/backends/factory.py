"""Built-in Lumora backend registrations and assembly."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
from typing import Any

from personal_agent.plugins.builtin.memory.lumora.backends.base import BackendSelection
from personal_agent.plugins.builtin.memory.lumora.backends.config import LumoraBackendConfig
from personal_agent.plugins.builtin.memory.lumora.backends.keyword import SqliteFts5KeywordIndex
from personal_agent.plugins.builtin.memory.lumora.backends.ranking import NoOpReranker, WeightedRrfFusion
from personal_agent.plugins.builtin.memory.lumora.backends.registry import BackendRegistry
from personal_agent.plugins.builtin.memory.lumora.embedding import (
    OpenAICompatibleEmbeddingBackend,
    OpenAICompatibleEmbeddingConfig,
)
from personal_agent.plugins.builtin.memory.lumora.qdrant_store import QdrantMemoryIndex, QdrantVectorConfig


embedding_registry = BackendRegistry("embedding")
vector_registry = BackendRegistry("vector")
keyword_registry = BackendRegistry("keyword")
fusion_registry = BackendRegistry("fusion")
reranker_registry = BackendRegistry("reranker")


@dataclass(frozen=True)
class LumoraBackends:
    config: LumoraBackendConfig
    embedding: Any
    vector: Any
    keyword: Any
    fusion: Any
    reranker: Any


def build_lumora_backends(*, context, archive) -> LumoraBackends:
    config = LumoraBackendConfig.from_options(context.provider_options)
    values = {"context": context, "archive": archive}
    return LumoraBackends(
        config=config,
        embedding=embedding_registry.create(
            config.embedding.provider, selection=config.embedding, **values
        ),
        vector=vector_registry.create(config.vector.provider, selection=config.vector, **values),
        keyword=keyword_registry.create(config.keyword.provider, selection=config.keyword, **values),
        fusion=fusion_registry.create(config.fusion.provider, selection=config.fusion, **values),
        reranker=reranker_registry.create(config.reranker.provider, selection=config.reranker, **values),
    )


def validate_lumora_backends(context) -> list[str]:
    try:
        config = LumoraBackendConfig.from_options(context.provider_options)
    except (TypeError, ValueError) as exc:
        return [str(exc)]
    errors: list[str] = []
    for registry, selection in (
        (embedding_registry, config.embedding),
        (vector_registry, config.vector),
        (keyword_registry, config.keyword),
        (fusion_registry, config.fusion),
        (reranker_registry, config.reranker),
    ):
        errors.extend(registry.validate(selection.provider, selection=selection, context=context))
    return errors


def _create_openai_embedding(*, selection: BackendSelection, context, **kwargs):
    del kwargs
    options = selection.options
    return OpenAICompatibleEmbeddingBackend(OpenAICompatibleEmbeddingConfig(
        base_url=str(options.get("base_url") or ""),
        api_key=context.get_env(str(options.get("api_key_env") or "")),
        model=str(options.get("model") or ""),
        dimensions=int(options.get("dimensions") or 0),
        timeout_seconds=float(options.get("timeout_seconds") or 30),
        batch_size=int(options.get("batch_size") or 10),
    ))


def _validate_openai_embedding(*, selection: BackendSelection, context) -> list[str]:
    options = selection.options
    errors = _required(options, "base_url", "model", "api_key_env")
    env_name = str(options.get("api_key_env") or "")
    if env_name and not context.get_env(env_name):
        errors.append(f"embedding environment variable is empty: {env_name}")
    if int(options.get("dimensions") or 0) < 0:
        errors.append("embedding dimensions must not be negative")
    if int(options.get("batch_size") or 10) <= 0:
        errors.append("embedding batch_size must be positive")
    return errors


def _create_qdrant(*, selection: BackendSelection, context, **kwargs):
    del kwargs
    options = selection.options
    env_name = str(options.get("api_key_env") or "")
    return QdrantMemoryIndex(QdrantVectorConfig(
        url=str(options.get("url") or ""),
        path=str(options.get("path") or ""),
        collection=str(options.get("collection") or "lumora_memories"),
        api_key=context.get_env(env_name) if env_name else "",
        timeout_seconds=float(options.get("timeout_seconds") or 10),
    ))


def _validate_qdrant(*, selection: BackendSelection, context) -> list[str]:
    del context
    options = selection.options
    errors: list[str] = []
    if bool(options.get("url")) == bool(options.get("path")):
        errors.append("qdrant requires exactly one of url or path")
    if not str(options.get("collection") or ""):
        errors.append("qdrant collection is required")
    if importlib.util.find_spec("qdrant_client") is None:
        errors.append("qdrant-client dependency is missing")
    return errors


def _create_sqlite_fts(*, selection: BackendSelection, archive, **kwargs):
    del kwargs
    return SqliteFts5KeywordIndex(
        archive,
        tokenizer=str(selection.options.get("tokenizer") or "unicode61"),
    )


def _create_weighted_rrf(*, selection: BackendSelection, **kwargs):
    del kwargs
    options = selection.options
    return WeightedRrfFusion(
        semantic_weight=float(options.get("semantic_weight", 0.6)),
        keyword_weight=float(options.get("keyword_weight", 0.4)),
        importance_weight=float(options.get("importance_weight", 0.1)),
        rrf_k=int(options.get("rrf_k", 60)),
    )


def _validate_weighted_rrf(*, selection: BackendSelection, context) -> list[str]:
    del context
    options = selection.options
    errors: list[str] = []
    if float(options.get("semantic_weight", 0.6)) < 0:
        errors.append("fusion semantic_weight must not be negative")
    if float(options.get("keyword_weight", 0.4)) < 0:
        errors.append("fusion keyword_weight must not be negative")
    if int(options.get("rrf_k", 60)) <= 0:
        errors.append("fusion rrf_k must be positive")
    return errors


def _create_none_reranker(**kwargs):
    del kwargs
    return NoOpReranker()


def _required(options: dict[str, Any], *names: str) -> list[str]:
    return [f"{name} is required" for name in names if not options.get(name)]


embedding_registry.register(
    "openai_compatible", _create_openai_embedding, validator=_validate_openai_embedding
)
vector_registry.register("qdrant", _create_qdrant, validator=_validate_qdrant)
keyword_registry.register("sqlite_fts5", _create_sqlite_fts)
fusion_registry.register("weighted_rrf", _create_weighted_rrf, validator=_validate_weighted_rrf)
reranker_registry.register("none", _create_none_reranker)
