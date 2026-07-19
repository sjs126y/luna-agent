"""Provider protocol and model capability catalog behavior."""

from luna_agent.llm.capabilities import resolve_api_mode, resolve_model_capability


def test_current_openai_model_keeps_hard_limit_but_uses_economical_default():
    capability = resolve_model_capability(
        "openai",
        "gpt-5.6-terra",
        configured_max_output_tokens=4096,
    )

    assert capability.model_context_limit == 1_050_000
    assert capability.effective_context_window == 256_000
    assert capability.model_max_output_tokens == 128_000
    assert capability.context_source == "provider-default"
    assert capability.capability_source == "openai-model-docs"


def test_explicit_context_is_clamped_to_verified_model_limit():
    capability = resolve_model_capability(
        "openai",
        "gpt-5.5",
        configured_context_window=1_000_000,
    )

    assert capability.model_context_limit == 400_000
    assert capability.effective_context_window == 400_000
    assert capability.context_clamped is True


def test_non_openai_provider_uses_verified_model_limit_by_default():
    capability = resolve_model_capability("deepseek", "deepseek-chat")

    assert capability.effective_context_window == 1_000_000
    assert capability.context_source == "deepseek-model-docs"


def test_unknown_model_uses_conservative_limit_and_clamps_override():
    capability = resolve_model_capability(
        "openrouter",
        "vendor/future-model",
        configured_context_window=1_000_000,
    )

    assert capability.model_context_limit == 256_000
    assert capability.effective_context_window == 256_000
    assert capability.context_clamped is True
    assert capability.capability_source == "conservative-fallback"


def test_explicit_model_marker_is_used_without_catalog_entry():
    capability = resolve_model_capability("openrouter", "vendor/custom-512k")

    assert capability.model_context_limit == 512_000
    assert capability.effective_context_window == 512_000
    assert capability.capability_source == "model-name-marker"


def test_max_output_is_clamped_to_verified_limit():
    capability = resolve_model_capability(
        "openai",
        "gpt-4.1",
        configured_max_output_tokens=100_000,
    )

    assert capability.effective_max_output_tokens == 32_768
    assert capability.output_clamped is True


def test_api_mode_resolution_precedence():
    assert resolve_api_mode(
        "openai",
        "https://proxy.example/v1",
        configured_mode="codex_responses",
    ).mode == "codex_responses"
    assert resolve_api_mode("openai", "https://proxy.example/v1").mode == "responses"
    assert resolve_api_mode("anthropic", "https://proxy.example/v1").mode == "anthropic_messages"
    assert resolve_api_mode("deepseek", "https://api.deepseek.com").mode == "anthropic_messages"
    assert resolve_api_mode("unknown", "https://api.anthropic.com").mode == "anthropic_messages"
