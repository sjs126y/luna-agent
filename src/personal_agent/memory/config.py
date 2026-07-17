"""Resolved configuration passed to memory providers."""

from __future__ import annotations

from dataclasses import dataclass, field
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
class MemoryProviderContext:
    requested_provider: str
    review: MemoryReviewConfig
    llm: MemoryLLMConfig
    provider_options: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, str] = field(default_factory=dict)

    def get_env(self, name: str, default: str = "") -> str:
        return str(self.environment.get(name, default) or default)


def resolve_memory_context(settings) -> MemoryProviderContext:
    requested = str(getattr(settings, "memory_external_provider", "none") or "none").lower()
    configured_llm = str(getattr(settings, "memory_llm_provider", "inherit") or "inherit")
    inherit = configured_llm == "inherit"
    llm_key = str(getattr(settings, "memory_llm_api_key", "") or "")
    if not llm_key:
        llm_key = str(getattr(settings, "llm_api_key", "") or "")
    options = getattr(settings, "memory_provider_options", {})
    selected_options = options.get(requested, {}) if isinstance(options, dict) else {}
    env_names = _environment_names(selected_options)
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
        provider_options=dict(selected_options) if isinstance(selected_options, dict) else {},
        environment={name: _settings_env(settings, name) for name in env_names},
    )


def _settings_env(settings, name: str) -> str:
    resolver = getattr(settings, "get_env", None)
    if not callable(resolver):
        return ""
    return str(resolver(name, "") or "")


def _environment_names(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).endswith("_env") and isinstance(item, str) and item:
                names.add(item)
            else:
                names.update(_environment_names(item))
    elif isinstance(value, list):
        for item in value:
            names.update(_environment_names(item))
    return names
