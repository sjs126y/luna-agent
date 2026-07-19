"""Provider-neutral configuration for Luna retrieval backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from luna_agent.plugins.builtin.memory.luna.backends.base import BackendSelection


@dataclass(frozen=True)
class RetrievalConfig:
    semantic_timeout_seconds: float = 30.0
    keyword_timeout_seconds: float = 5.0
    reranker_timeout_seconds: float = 30.0


@dataclass(frozen=True)
class LunaBackendConfig:
    embedding: BackendSelection
    vector: BackendSelection
    keyword: BackendSelection
    fusion: BackendSelection
    reranker: BackendSelection
    retrieval: RetrievalConfig

    @classmethod
    def from_options(cls, value: dict[str, Any] | None) -> "LunaBackendConfig":
        options = dict(value or {})
        return cls(
            embedding=_selection(options.get("embedding"), "openai_compatible"),
            vector=_selection(options.get("vector"), "qdrant"),
            keyword=_selection(options.get("keyword"), "sqlite_fts5"),
            fusion=_selection(options.get("fusion"), "weighted_rrf"),
            reranker=_selection(options.get("reranker"), "none"),
            retrieval=_retrieval(options.get("retrieval")),
        )


def _selection(value: Any, default_provider: str) -> BackendSelection:
    data = dict(value) if isinstance(value, dict) else {}
    provider = str(data.pop("provider", default_provider) or default_provider)
    return BackendSelection(provider=provider, options=data)


def _retrieval(value: Any) -> RetrievalConfig:
    data = dict(value) if isinstance(value, dict) else {}
    return RetrievalConfig(
        semantic_timeout_seconds=_positive_float(data, "semantic_timeout_seconds", 30.0),
        keyword_timeout_seconds=_positive_float(data, "keyword_timeout_seconds", 5.0),
        reranker_timeout_seconds=_positive_float(data, "reranker_timeout_seconds", 30.0),
    )


def _positive_float(data: dict[str, Any], key: str, default: float) -> float:
    value = float(data.get(key, default))
    if value <= 0:
        raise ValueError(f"memory.providers.luna.retrieval.{key} must be positive")
    return value
