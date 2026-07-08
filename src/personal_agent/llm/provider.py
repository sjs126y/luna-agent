"""Provider profile — 'who to talk to' vs Transport's 'how to talk'."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

CacheStrategy = str


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
    context_window: int = 0                # 0 = auto-detect from model name
    reasoning_effort: str = ""             # provider-specific effort, empty = omit

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


def _detect_context_window(model: str) -> int:
    """Infer context window from model name. Conservative estimates."""
    m = model.lower()
    if "1m" in m or "1.0m" in m:
        return 1_000_000
    if "200k" in m or "claude" in m:
        return 200_000
    if "128k" in m or "gpt-4" in m or "gpt-4o" in m:
        return 128_000
    if "32k" in m:
        return 32_000
    if "deepseek" in m:  # all DeepSeek models = 1M (v4, r1, chat)
        return 1_000_000
    return 64_000


def _configured_context_window(config) -> int:
    configured = int(getattr(config, "llm_context_window", 0) or 0)
    if configured > 0:
        return configured
    return _detect_context_window(str(getattr(config, "llm_model", "") or ""))


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
        return self._factories[name](config)

    def list(self) -> list[str]:
        return list(self._factories.keys())

    @staticmethod
    def detect_api_mode(base_url: str, provider_name: str) -> str:
        """Infer api_mode from base_url; explicit LLM_API_MODE wins."""
        import os
        explicit = os.getenv("LLM_API_MODE", "auto")
        if explicit != "auto":
            return explicit

        url_lower = base_url.lower()
        if "anthropic" in url_lower:
            return "anthropic_messages"
        if "openai" in url_lower or "openrouter" in url_lower:
            return "chat_completions"
        if provider_name == "deepseek" and "anthropic" in url_lower:
            return "anthropic_messages"
        return "chat_completions"


# Module-level singleton
provider_registry = ProviderRegistry()


# ── Builtin provider factories ─────────────────────────

def _deepseek_factory(config) -> ProviderProfile:
    return ProviderProfile(
        name="deepseek", base_url=config.llm_base_url, api_key=config.llm_api_key,
        model=config.llm_model, max_tokens=config.llm_max_tokens,
        context_window=_configured_context_window(config),
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
        model=config.llm_model, max_tokens=config.llm_max_tokens,
        context_window=_configured_context_window(config),
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
        model=config.llm_model, max_tokens=config.llm_max_tokens,
        context_window=_configured_context_window(config),
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
        max_tokens=config.llm_max_tokens,
        context_window=_configured_context_window(config),
        reasoning_effort=_configured_reasoning_effort(config),
        extra_headers={
            "HTTP-Referer": "http://localhost",
            "X-Title": "Personal Agent",
        },
        cache_strategy="prefix",
        supports_cache_usage=True,
        cache_usage_fields={
            "cache_hit_tokens": "prompt_tokens_details.cached_tokens",
        },
        cacheable_blocks=("system", "tools", "message_prefix"),
    )


provider_registry.register("deepseek", _deepseek_factory)
provider_registry.register("openai", _openai_factory)
provider_registry.register("anthropic", _anthropic_factory)
provider_registry.register("openrouter", _openrouter_factory)
