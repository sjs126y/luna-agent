"""Resolved configuration passed to memory providers."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any


@dataclass(frozen=True)
class MemoryReviewConfig:
    external_turn_interval: int = 10
    internal_turn_interval: int = 50
    internal_buffer_limit: int = 20
    snapshot_refresh_turn_interval: int = 20
    worker_concurrency: int = 2


@dataclass(frozen=True)
class MemoryLLMConfig:
    provider: str
    model: str
    base_url: str
    api_key: str
    api_mode: str
    max_tokens: int


@dataclass(frozen=True)
class EmbeddingConfig:
    api_mode: str
    base_url: str
    api_key: str
    model: str
    dimensions: int


@dataclass(frozen=True)
class QdrantConfig:
    url: str
    collection: str
    api_key: str
    timeout_seconds: int


@dataclass(frozen=True)
class MemoryProviderContext:
    requested_provider: str
    review: MemoryReviewConfig
    llm: MemoryLLMConfig
    embedding: EmbeddingConfig
    qdrant: QdrantConfig
    provider_options: dict[str, Any] = field(default_factory=dict)


def resolve_memory_context(settings) -> MemoryProviderContext:
    requested = str(getattr(settings, "memory_external_provider", "none") or "none").lower()
    configured_llm = str(getattr(settings, "memory_llm_provider", "inherit") or "inherit")
    inherit = configured_llm == "inherit"
    env = {**getattr(settings, "raw_env", {}), **os.environ}
    llm_key = str(getattr(settings, "memory_llm_api_key", "") or "")
    if not llm_key:
        llm_key = str(getattr(settings, "llm_api_key", "") or "")
    embedding_key = str(getattr(settings, "memory_embedding_api_key", "") or "")
    if not embedding_key:
        embedding_key = str(env.get(getattr(settings, "memory_embedding_api_key_env", "DASHSCOPE_API_KEY"), ""))
    qdrant_key = str(getattr(settings, "memory_qdrant_api_key", "") or "")
    if not qdrant_key:
        qdrant_key = str(env.get(getattr(settings, "memory_qdrant_api_key_env", "QDRANT_API_KEY"), ""))
    options = getattr(settings, "memory_provider_options", {})
    selected_options = options.get(requested, {}) if isinstance(options, dict) else {}
    return MemoryProviderContext(
        requested_provider=requested,
        review=MemoryReviewConfig(
            external_turn_interval=int(getattr(settings, "memory_external_turn_interval", 10)),
            internal_turn_interval=int(getattr(settings, "memory_internal_turn_interval", 50)),
            internal_buffer_limit=int(getattr(settings, "memory_internal_buffer_limit", 20)),
            snapshot_refresh_turn_interval=int(getattr(settings, "memory_snapshot_refresh_turn_interval", 20)),
            worker_concurrency=int(getattr(settings, "memory_worker_concurrency", 2)),
        ),
        llm=MemoryLLMConfig(
            provider=str(getattr(settings, "llm_provider", "") if inherit else configured_llm),
            model=str(getattr(settings, "llm_model", "") if inherit else getattr(settings, "memory_llm_model", "")),
            base_url=str(getattr(settings, "llm_base_url", "") if inherit else getattr(settings, "memory_llm_base_url", "")),
            api_key=llm_key,
            api_mode=str(getattr(settings, "llm_api_mode", "auto") if inherit else getattr(settings, "memory_llm_api_mode", "auto")),
            max_tokens=int(getattr(settings, "memory_llm_max_tokens", 2048)),
        ),
        embedding=EmbeddingConfig(
            api_mode=str(getattr(settings, "memory_embedding_api_mode", "openai_compatible")),
            base_url=str(getattr(settings, "memory_embedding_base_url", "")),
            api_key=embedding_key,
            model=str(getattr(settings, "memory_embedding_model", "")),
            dimensions=int(getattr(settings, "memory_embedding_dimensions", 0)),
        ),
        qdrant=QdrantConfig(
            url=str(getattr(settings, "memory_qdrant_url", "")),
            collection=str(getattr(settings, "memory_qdrant_collection", "lumora_memories")),
            api_key=qdrant_key,
            timeout_seconds=int(getattr(settings, "memory_qdrant_timeout_seconds", 10)),
        ),
        provider_options=dict(selected_options) if isinstance(selected_options, dict) else {},
    )
