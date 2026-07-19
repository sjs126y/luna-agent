"""Provider profile — 'who to talk to' vs Transport's 'how to talk'."""

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
from typing import Any

from luna_agent.llm.capabilities import (
    ResolvedModelCapability,
    detect_context_window,
    resolve_api_mode,
    resolve_model_capability,
)

CacheStrategy = str
logger = logging.getLogger(__name__)


@dataclass
class ProviderProfile:
    """Same ChatCompletionsTransport can serve 16+ OpenAI-compatible vendors.
    Differences live in request_hook / response_hook.
    """
    name: str                              # "deepseek", "openai", "anthropic"
    base_url: str
    api_key: str
    model: str
    max_tokens: int = 4096
    context_window: int = 0                # Effective window used by the Agent.
    reasoning_effort: str = ""             # provider-specific effort, empty = omit
    model_context_limit: int = 0            # Documented or conservative hard limit.
    model_max_output_tokens: int = 0        # 0 = capability is not verified.
    context_source: str = ""
    output_source: str = ""
    capability_source: str = ""
    capability_verified_at: str = ""
    context_clamped: bool = False
    output_clamped: bool = False
    api_mode: str = ""
    api_mode_source: str = ""

    # Hooks to patch vendor quirks (e.g., a vendor doesn't support temperature)
    request_hook: Callable[[dict], dict] | None = None
    response_hook: Callable[[dict], dict] | None = None

    extra_headers: dict[str, str] = field(default_factory=dict)

    # Provider cache capability. Transports use this for diagnostics and
    # provider-specific usage normalization; v1 does not change request layout.
    cache_strategy: CacheStrategy = "none"          # none | prefix | explicit
    supports_cache_usage: bool = False
    cache_usage_fields: dict[str, str] = field(default_factory=dict)
    cacheable_blocks: tuple[str, ...] = ()

    # Multimodal capability. Conservative defaults keep text-only providers safe.
    supports_image_input: bool = False
    image_input_modes: tuple[str, ...] = ()
    supported_image_mime_types: tuple[str, ...] = ()
    max_image_bytes: int = 0

    def cache_capability(self) -> dict[str, Any]:
        return {
            "strategy": self.cache_strategy,
            "supports_usage": self.supports_cache_usage,
            "usage_fields": dict(self.cache_usage_fields),
            "cacheable_blocks": list(self.cacheable_blocks),
        }

    def multimodal_capability(self) -> dict[str, Any]:
        return {
            "supports_image_input": self.supports_image_input,
            "image_input_modes": list(self.image_input_modes),
            "supported_image_mime_types": list(self.supported_image_mime_types),
            "max_image_bytes": self.max_image_bytes,
        }

    def model_capability(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "effective_context_window": self.context_window,
            "model_context_limit": self.model_context_limit,
            "effective_max_output_tokens": self.max_tokens,
            "model_max_output_tokens": self.model_max_output_tokens,
            "context_source": self.context_source,
            "output_source": self.output_source,
            "capability_source": self.capability_source,
            "capability_verified_at": self.capability_verified_at,
            "context_clamped": self.context_clamped,
            "output_clamped": self.output_clamped,
            "api_mode": self.api_mode,
            "api_mode_source": self.api_mode_source,
        }


def _detect_context_window(model: str) -> int:
    """Compatibility wrapper for consumers that have no provider config."""
    return detect_context_window(model)


def _configured_capability(config, provider_name: str) -> ResolvedModelCapability:
    capability = resolve_model_capability(
        provider_name,
        str(getattr(config, "llm_model", "") or ""),
        configured_context_window=int(getattr(config, "llm_context_window", 0) or 0),
        configured_max_output_tokens=int(getattr(config, "llm_max_tokens", 4096) or 4096),
    )
    if capability.context_clamped:
        logger.warning(
            "Configured context window exceeds known model limit; clamped: "
            "provider=%s model=%s configured=%s limit=%s",
            provider_name,
            capability.model,
            getattr(config, "llm_context_window", 0),
            capability.model_context_limit,
        )
    if capability.output_clamped:
        logger.warning(
            "Configured max output exceeds known model limit; clamped: "
            "provider=%s model=%s configured=%s limit=%s",
            provider_name,
            capability.model,
            getattr(config, "llm_max_tokens", 4096),
            capability.model_max_output_tokens,
        )
    return capability


def _capability_fields(config, provider_name: str) -> dict[str, Any]:
    resolved = _configured_capability(config, provider_name)
    return {
        "max_tokens": resolved.effective_max_output_tokens,
        "context_window": resolved.effective_context_window,
        "model_context_limit": resolved.model_context_limit,
        "model_max_output_tokens": resolved.model_max_output_tokens,
        "context_source": resolved.context_source,
        "output_source": resolved.output_source,
        "capability_source": resolved.capability_source,
        "capability_verified_at": resolved.capability_verified_at,
        "context_clamped": resolved.context_clamped,
        "output_clamped": resolved.output_clamped,
    }


def _configured_reasoning_effort(config) -> str:
    return str(getattr(config, "llm_reasoning_effort", "") or "").strip()


# ── Provider Registry ──────────────────────────────────

class ProviderRegistry:
    """Global registry of LLM providers. Each provider registers its default
    profile factory so Gateway can resolve providers by name at runtime.
    """

    def __init__(self) -> None:
        self._factories: dict[str, callable] = {}

    def register(self, name: str, factory: callable) -> None:
        self._factories[name] = factory

    def get(self, name: str, config) -> ProviderProfile:
        if name not in self._factories:
            raise KeyError(f"Unknown provider: {name}. Registered: {list(self._factories)}")
        profile = self._factories[name](config)
        configured = str(getattr(config, "llm_api_mode", "auto") or "auto").strip()
        if configured and configured != "auto":
            profile.api_mode = configured
            profile.api_mode_source = "configured"
        else:
            profile.api_mode = self.detect_api_mode(profile.base_url, name)
            profile.api_mode_source = resolve_api_mode(name, profile.base_url).source
        return profile

    def list(self) -> list[str]:
        return list(self._factories.keys())

    @staticmethod
    def detect_api_mode(base_url: str, provider_name: str) -> str:
        """Infer api_mode from provider defaults and clear endpoint metadata."""
        return resolve_api_mode(provider_name, base_url).mode


# Module-level singleton
provider_registry = ProviderRegistry()


# ── Builtin provider factories ─────────────────────────

def _deepseek_factory(config) -> ProviderProfile:
    return ProviderProfile(
        name="deepseek", base_url=config.llm_base_url, api_key=config.llm_api_key,
        model=config.llm_model,
        **_capability_fields(config, "deepseek"),
        reasoning_effort=_configured_reasoning_effort(config),
        cache_strategy="prefix",
        supports_cache_usage=True,
        cache_usage_fields={
            "cache_hit_tokens": "prompt_cache_hit_tokens",
            "cache_miss_tokens": "prompt_cache_miss_tokens",
        },
        cacheable_blocks=("system", "tools", "message_prefix"),
    )

def _openai_factory(config) -> ProviderProfile:
    return ProviderProfile(
        name="openai", base_url=config.llm_base_url, api_key=config.llm_api_key,
        model=config.llm_model,
        **_capability_fields(config, "openai"),
        reasoning_effort=_configured_reasoning_effort(config),
        cache_strategy="prefix",
        supports_cache_usage=True,
        cache_usage_fields={
            "cache_hit_tokens": "prompt_tokens_details.cached_tokens",
        },
        cacheable_blocks=("system", "tools", "message_prefix"),
        supports_image_input=True,
        image_input_modes=("url", "base64"),
        supported_image_mime_types=("image/jpeg", "image/png", "image/webp", "image/gif"),
        max_image_bytes=20 * 1024 * 1024,
    )

def _anthropic_factory(config) -> ProviderProfile:
    return ProviderProfile(
        name="anthropic", base_url=config.llm_base_url, api_key=config.llm_api_key,
        model=config.llm_model,
        **_capability_fields(config, "anthropic"),
        reasoning_effort=_configured_reasoning_effort(config),
        cache_strategy="explicit",
        supports_cache_usage=True,
        cache_usage_fields={
            "cache_write_tokens": "cache_creation_input_tokens",
            "cache_read_tokens": "cache_read_input_tokens",
        },
        cacheable_blocks=("system",),
        supports_image_input=True,
        image_input_modes=("base64",),
        supported_image_mime_types=("image/jpeg", "image/png", "image/webp", "image/gif"),
        max_image_bytes=5 * 1024 * 1024,
    )

def _openrouter_factory(config) -> ProviderProfile:
    return ProviderProfile(
        name="openrouter",
        base_url=config.llm_base_url if "openrouter" in (config.llm_base_url or "").lower()
                 else "https://openrouter.ai/api/v1",
        api_key=config.llm_api_key,
        model=config.llm_model,
        **_capability_fields(config, "openrouter"),
        reasoning_effort=_configured_reasoning_effort(config),
        extra_headers={
            "HTTP-Referer": "http://localhost",
            "X-Title": "Luna Agent",
        },
        cache_strategy="prefix",
        supports_cache_usage=True,
        cache_usage_fields={
            "cache_hit_tokens": "prompt_tokens_details.cached_tokens",
        },
        cacheable_blocks=("system", "tools", "message_prefix"),
    )


def _xai_factory(config) -> ProviderProfile:
    """Create an xAI profile for its OpenAI-compatible Chat Completions API."""
    return ProviderProfile(
        name="xai",
        base_url=config.llm_base_url or "https://api.x.ai/v1",
        api_key=config.llm_api_key,
        model=config.llm_model,
        **_capability_fields(config, "xai"),
        reasoning_effort=_configured_reasoning_effort(config),
        supports_image_input=True,
        image_input_modes=("url", "base64"),
        supported_image_mime_types=("image/jpeg", "image/png", "image/webp", "image/gif"),
        max_image_bytes=20 * 1024 * 1024,
    )


provider_registry.register("deepseek", _deepseek_factory)
provider_registry.register("openai", _openai_factory)
provider_registry.register("anthropic", _anthropic_factory)
provider_registry.register("openrouter", _openrouter_factory)
provider_registry.register("xai", _xai_factory)
