"""Provider protocol defaults and model capability resolution."""

from __future__ import annotations

from dataclasses import dataclass
import re


UNKNOWN_MODEL_CONTEXT_LIMIT = 256_000
OPENAI_DEFAULT_EFFECTIVE_CONTEXT = 256_000


@dataclass(frozen=True)
class ProviderCapability:
    """Stable provider-level behavior that does not depend on a model."""

    name: str
    default_api_mode: str
    default_effective_context: int = 0


@dataclass(frozen=True)
class ModelCapability:
    """A versioned model-family limit from provider documentation."""

    pattern: str
    context_window: int
    max_output_tokens: int
    source: str
    verified_at: str
    providers: tuple[str, ...] = ()

    def matches(self, provider_name: str, model: str) -> bool:
        if self.providers and provider_name not in self.providers:
            return False
        return re.search(self.pattern, model, re.IGNORECASE) is not None


@dataclass(frozen=True)
class ResolvedModelCapability:
    """Effective limits after applying configuration and conservative fallbacks."""

    provider_name: str
    model: str
    model_context_limit: int
    effective_context_window: int
    model_max_output_tokens: int
    effective_max_output_tokens: int
    context_source: str
    output_source: str
    capability_source: str
    capability_verified_at: str
    context_clamped: bool = False
    output_clamped: bool = False


@dataclass(frozen=True)
class ApiModeResolution:
    mode: str
    source: str


PROVIDER_CAPABILITIES: dict[str, ProviderCapability] = {
    "openai": ProviderCapability(
        name="openai",
        default_api_mode="responses",
        default_effective_context=OPENAI_DEFAULT_EFFECTIVE_CONTEXT,
    ),
    "anthropic": ProviderCapability(name="anthropic", default_api_mode="anthropic_messages"),
    "deepseek": ProviderCapability(name="deepseek", default_api_mode="anthropic_messages"),
    "openrouter": ProviderCapability(name="openrouter", default_api_mode="chat_completions"),
    "xai": ProviderCapability(name="xai", default_api_mode="chat_completions"),
}


# Ordered from exact/current families to broad legacy families. The catalog is
# intentionally small: an unverified name receives a conservative 256K limit.
MODEL_CAPABILITIES: tuple[ModelCapability, ...] = (
    ModelCapability(
        pattern=r"(?:^|/)(?:gpt-5\.6(?:-(?:sol|terra|luna))?)$",
        context_window=1_050_000,
        max_output_tokens=128_000,
        source="openai-model-docs",
        verified_at="2026-07-19",
    ),
    ModelCapability(
        pattern=r"(?:^|/)gpt-5(?:[.-](?:[0-5]|mini|nano|pro|codex).*)?$",
        context_window=400_000,
        max_output_tokens=128_000,
        source="openai-model-docs",
        verified_at="2026-07-19",
    ),
    ModelCapability(
        pattern=r"(?:^|/)gpt-4\.1(?:-(?:mini|nano))?$",
        context_window=1_000_000,
        max_output_tokens=32_768,
        source="openai-model-docs",
        verified_at="2026-07-19",
    ),
    ModelCapability(
        pattern=r"(?:^|/)gpt-4o(?:-.*)?$",
        context_window=128_000,
        max_output_tokens=16_384,
        source="openai-model-docs",
        verified_at="2026-07-19",
    ),
    ModelCapability(
        pattern=r"(?:^|/)(?:o1|o3|o4)(?:-.*)?$",
        context_window=200_000,
        max_output_tokens=100_000,
        source="openai-model-docs",
        verified_at="2026-07-19",
    ),
    ModelCapability(
        pattern=r"(?:^|/)claude(?:-.*)?$",
        context_window=200_000,
        max_output_tokens=64_000,
        source="anthropic-model-docs",
        verified_at="2026-07-19",
    ),
    ModelCapability(
        pattern=r"(?:^|/)deepseek-(?:chat|reasoner|v4(?:-.*)?)$",
        context_window=1_000_000,
        max_output_tokens=384_000,
        source="deepseek-model-docs",
        verified_at="2026-07-19",
    ),
    ModelCapability(
        pattern=r"(?:^|/)qwen3\.(?:5|6|7)-(?:plus|max|flash|coder-plus)(?:-.*)?$",
        context_window=1_000_000,
        max_output_tokens=65_536,
        source="alibaba-model-studio-docs",
        verified_at="2026-07-19",
    ),
    ModelCapability(
        pattern=r"(?:^|/)qwen3(?:-coder)?-(?:next|plus)(?:-.*)?$",
        context_window=1_000_000,
        max_output_tokens=65_536,
        source="alibaba-model-studio-docs",
        verified_at="2026-07-19",
    ),
    ModelCapability(
        pattern=r"(?:^|/)gemini-(?:3|3\.1|2\.5)(?:-.*)?$",
        context_window=1_000_000,
        max_output_tokens=65_536,
        source="google-gemini-model-docs",
        verified_at="2026-07-19",
    ),
)


def provider_capability(provider_name: str) -> ProviderCapability | None:
    return PROVIDER_CAPABILITIES.get(_normalize_provider(provider_name))


def find_model_capability(provider_name: str, model: str) -> ModelCapability | None:
    provider = _normalize_provider(provider_name)
    normalized_model = str(model or "").strip()
    for capability in MODEL_CAPABILITIES:
        if capability.matches(provider, normalized_model):
            return capability
    return None


def resolve_model_capability(
    provider_name: str,
    model: str,
    *,
    configured_context_window: int = 0,
    configured_max_output_tokens: int = 4096,
) -> ResolvedModelCapability:
    provider = _normalize_provider(provider_name)
    model_name = str(model or "").strip()
    catalog_entry = find_model_capability(provider, model_name)
    marker_limit = _context_marker_limit(model_name)

    if catalog_entry is not None:
        hard_context = catalog_entry.context_window
        hard_output = catalog_entry.max_output_tokens
        capability_source = catalog_entry.source
        verified_at = catalog_entry.verified_at
    elif marker_limit:
        hard_context = marker_limit
        hard_output = 0
        capability_source = "model-name-marker"
        verified_at = ""
    else:
        hard_context = UNKNOWN_MODEL_CONTEXT_LIMIT
        hard_output = 0
        capability_source = "conservative-fallback"
        verified_at = ""

    configured_context = max(0, int(configured_context_window or 0))
    if configured_context:
        effective_context = min(configured_context, hard_context)
        context_source = "configured"
        context_clamped = configured_context > hard_context
    else:
        provider_default = provider_capability(provider)
        economical_default = int(getattr(provider_default, "default_effective_context", 0) or 0)
        if economical_default:
            effective_context = min(economical_default, hard_context)
            context_source = "provider-default"
        else:
            effective_context = hard_context
            context_source = capability_source
        context_clamped = False

    requested_output = max(1, int(configured_max_output_tokens or 4096))
    if hard_output > 0:
        effective_output = min(requested_output, hard_output)
        output_clamped = requested_output > hard_output
        output_source = "configured" if not output_clamped else "configured-clamped"
    else:
        effective_output = requested_output
        output_clamped = False
        output_source = "configured-unverified"

    return ResolvedModelCapability(
        provider_name=provider,
        model=model_name,
        model_context_limit=hard_context,
        effective_context_window=effective_context,
        model_max_output_tokens=hard_output,
        effective_max_output_tokens=effective_output,
        context_source=context_source,
        output_source=output_source,
        capability_source=capability_source,
        capability_verified_at=verified_at,
        context_clamped=context_clamped,
        output_clamped=output_clamped,
    )


def resolve_api_mode(
    provider_name: str,
    base_url: str,
    *,
    configured_mode: str = "auto",
) -> ApiModeResolution:
    configured = str(configured_mode or "auto").strip().lower()
    if configured and configured != "auto":
        return ApiModeResolution(configured, "configured")

    provider = provider_capability(provider_name)
    if provider is not None:
        return ApiModeResolution(provider.default_api_mode, "provider-default")

    url = str(base_url or "").lower()
    if "anthropic" in url:
        return ApiModeResolution("anthropic_messages", "base-url")
    if "openai.com" in url and "/v1" in url:
        return ApiModeResolution("responses", "base-url")
    if "openrouter" in url:
        return ApiModeResolution("chat_completions", "base-url")
    return ApiModeResolution("chat_completions", "fallback")


def detect_context_window(model: str) -> int:
    """Compatibility helper for call sites without a provider profile."""
    capability = find_model_capability("", model)
    if capability is not None:
        return capability.context_window
    return _context_marker_limit(model) or UNKNOWN_MODEL_CONTEXT_LIMIT


def _context_marker_limit(model: str) -> int:
    value = str(model or "").lower()
    marker_pattern = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*([mk])(?:\b|[^a-z])")
    for match in marker_pattern.finditer(value):
        amount = float(match.group(1))
        multiplier = 1_000_000 if match.group(2) == "m" else 1_000
        tokens = int(amount * multiplier)
        if tokens >= 8_000:
            return tokens
    return 0


def _normalize_provider(provider_name: str) -> str:
    return str(provider_name or "").strip().lower()
