"""Provider cache capability, usage normalization, and request diagnostics."""

from __future__ import annotations

from types import SimpleNamespace

from luna_agent.llm.base import LLMRequestPlan
from luna_agent.llm.provider import ProviderProfile, provider_registry
from luna_agent.plugins.builtin.llm.builtin.anthropic import AnthropicMessagesTransport
from luna_agent.plugins.builtin.llm.builtin.chat_completions import ChatCompletionsTransport


def _settings(provider: str, model: str = "m"):
    return SimpleNamespace(
        llm_provider=provider,
        llm_base_url="https://example.test",
        llm_api_key="k",
        llm_model=model,
        llm_max_tokens=4096,
        llm_context_window=0,
        llm_reasoning_effort="",
    )


def test_builtin_provider_cache_capabilities():
    anthropic = provider_registry.get("anthropic", _settings("anthropic", "claude-3-5-sonnet"))
    deepseek = provider_registry.get("deepseek", _settings("deepseek", "deepseek-chat"))
    openai = provider_registry.get("openai", _settings("openai", "gpt-4o"))
    openrouter = provider_registry.get("openrouter", _settings("openrouter", "openai/gpt-4o"))
    xai = provider_registry.get("xai", _settings("xai", "grok-4.5"))

    assert anthropic.cache_capability()["strategy"] == "explicit"
    assert anthropic.supports_cache_usage is True
    assert "system" in anthropic.cacheable_blocks
    assert deepseek.cache_strategy == "prefix"
    assert deepseek.cache_usage_fields["cache_hit_tokens"] == "prompt_cache_hit_tokens"
    assert openai.cache_usage_fields["cache_hit_tokens"] == "prompt_tokens_details.cached_tokens"
    assert openrouter.cache_strategy == "prefix"
    assert xai.name == "xai"
    assert xai.supports_image_input is True
    assert xai.image_input_modes == ("url", "base64")


def test_xai_provider_uses_official_default_base_url():
    settings = _settings("xai", "grok-4.5")
    settings.llm_base_url = ""

    provider = provider_registry.get("xai", settings)

    assert provider.base_url == "https://api.x.ai/v1"
    assert provider.model == "grok-4.5"


def test_provider_context_window_uses_configured_override():
    settings = _settings("openai", "gpt-5.5")
    settings.llm_context_window = 1_000_000

    provider = provider_registry.get("openai", settings)

    assert provider.context_window == 1_000_000


def test_provider_context_window_falls_back_to_model_detection():
    provider = provider_registry.get("openai", _settings("openai", "gpt-5.5"))

    assert provider.context_window == 400_000


def test_provider_reasoning_effort_uses_env_setting():
    settings = _settings("openai", "gpt-5.5")
    settings.llm_reasoning_effort = "high"

    provider = provider_registry.get("openai", settings)

    assert provider.reasoning_effort == "high"


def test_anthropic_usage_normalizes_cache_fields():
    provider = ProviderProfile(
        name="anthropic",
        base_url="https://example.test",
        api_key="k",
        model="claude",
        cache_strategy="explicit",
        supports_cache_usage=True,
        cache_usage_fields={
            "cache_write_tokens": "cache_creation_input_tokens",
            "cache_read_tokens": "cache_read_input_tokens",
        },
    )
    transport = AnthropicMessagesTransport(provider)

    usage = transport.normalize_usage({
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 40,
    })

    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 20
    assert usage["cache_hit_tokens"] == 40
    assert usage["cache_read_tokens"] == 40
    assert usage["cache_write_tokens"] == 30
    assert usage["cache_miss_tokens"] == 60
    assert usage["cache_hit_rate"] == 0.4


def test_deepseek_usage_normalizes_cache_fields():
    provider = provider_registry.get("deepseek", _settings("deepseek", "deepseek-chat"))
    transport = ChatCompletionsTransport(provider)

    usage = transport.normalize_usage({
        "prompt_tokens": 100,
        "completion_tokens": 5,
        "prompt_cache_hit_tokens": 75,
        "prompt_cache_miss_tokens": 25,
    })

    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 5
    assert usage["cache_hit_tokens"] == 75
    assert usage["cache_miss_tokens"] == 25
    assert usage["cache_read_tokens"] == 75
    assert usage["cache_hit_rate"] == 0.75


def test_openai_cached_tokens_are_normalized():
    provider = provider_registry.get("openai", _settings("openai", "gpt-4o"))
    transport = ChatCompletionsTransport(provider)

    usage = transport.normalize_usage({
        "prompt_tokens": 200,
        "completion_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 80},
    })

    assert usage["input_tokens"] == 200
    assert usage["output_tokens"] == 10
    assert usage["cache_hit_tokens"] == 80
    assert usage["cache_read_tokens"] == 80
    assert usage["cache_miss_tokens"] == 120
    assert usage["cache_hit_rate"] == 0.4
    assert usage["cache_usage_reported"] is True


def test_missing_provider_cache_fields_are_not_reported_as_zero_hit_evidence():
    provider = provider_registry.get("openai", _settings("openai", "gpt-test"))
    transport = ChatCompletionsTransport(provider)

    usage = transport.normalize_usage({
        "prompt_tokens": 200,
        "completion_tokens": 10,
    })

    assert usage["cache_hit_tokens"] == 0
    assert usage["cache_hit_rate"] == 0.0
    assert usage["cache_usage_reported"] is False


def test_cache_diagnostics_hash_stability_for_system_and_tools():
    provider = provider_registry.get("deepseek", _settings("deepseek", "deepseek-chat"))
    transport = ChatCompletionsTransport(provider)
    tools = [{"name": "read", "description": "Read", "input_schema": {"type": "object"}}]
    body_a = transport.build_request(
        [{"role": "user", "content": [{"type": "text", "text": "first"}]}],
        "system",
        tools,
        100,
    )
    body_b = transport.build_request(
        [{"role": "user", "content": [{"type": "text", "text": "second"}]}],
        "system",
        tools,
        100,
    )

    diag_a = transport.cache_diagnostics(body_a)
    diag_b = transport.cache_diagnostics(body_b)

    assert diag_a["system_hash"] == diag_b["system_hash"]
    assert diag_a["tools_hash"] == diag_b["tools_hash"]
    assert diag_a["message_prefix_hash"] == diag_b["message_prefix_hash"]
    assert diag_a["stable_prefix_hash"] == diag_b["stable_prefix_hash"]
    assert diag_a["message_count"] == 2
    assert diag_a["tool_count"] == 1


def test_anthropic_build_request_only_marks_system_cache_by_default():
    provider = provider_registry.get("anthropic", _settings("anthropic", "claude-3-5-sonnet"))
    transport = AnthropicMessagesTransport(provider)

    body = transport.build_request(
        [{"role": "user", "content": [{"type": "text", "text": "dynamic"}]}],
        "stable system",
        [],
        100,
    )

    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in body["messages"][-1]["content"][-1]


def test_chat_completions_tools_are_sorted_without_cache_fields():
    provider = provider_registry.get("deepseek", _settings("deepseek", "deepseek-chat"))
    transport = ChatCompletionsTransport(provider)

    body = transport.build_request(
        [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "system",
        [
            {"name": "zeta", "description": "", "input_schema": {}},
            {"name": "alpha", "description": "", "input_schema": {}},
        ],
        100,
    )

    assert [item["function"]["name"] for item in body["tools"]] == ["alpha", "zeta"]
    assert "cache_control" not in body
    assert all("cache_control" not in item for item in body["tools"])


def test_chat_completions_request_includes_reasoning_effort_when_configured():
    settings = _settings("openai", "gpt-5.5")
    settings.llm_reasoning_effort = "high"
    provider = provider_registry.get("openai", settings)
    transport = ChatCompletionsTransport(provider)

    body = transport.build_request(
        [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "system",
        [],
        100,
    )

    assert body["reasoning_effort"] == "high"


def test_request_plan_orders_dynamic_history_and_current_user():
    plan = LLMRequestPlan(
        stable_system="system",
        stable_tools=[{"name": "read"}],
        stable_context=[{"role": "user", "content": "stable"}],
        dynamic_context=[{"role": "user", "content": "dynamic"}],
        history=[{"role": "assistant", "content": "old"}],
        current_user={"role": "user", "content": "now"},
    )

    assert plan.to_messages() == [
        {"role": "user", "content": "stable"},
        {"role": "user", "content": "dynamic"},
        {"role": "assistant", "content": "old"},
        {"role": "user", "content": "now"},
    ]
    diagnostics = plan.diagnostics()
    assert diagnostics["stable_block_count"] == 3
    assert diagnostics["dynamic_block_count"] == 1
    assert diagnostics["current_user_present"] is True


def test_build_request_from_plan_matches_legacy_request_for_simple_input():
    provider = provider_registry.get("deepseek", _settings("deepseek", "deepseek-chat"))
    transport = ChatCompletionsTransport(provider)
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    tools = [{"name": "read", "description": "", "input_schema": {}}]
    plan = LLMRequestPlan.from_legacy(messages, "system", tools)

    assert transport.build_request_from_plan(plan, 100) == transport.build_request(
        messages,
        "system",
        tools,
        100,
    )


def test_request_plan_stable_hash_ignores_current_user_changes():
    base = LLMRequestPlan(
        stable_system="system",
        stable_tools=[{"name": "read"}],
        dynamic_context=[{"role": "user", "content": "memory"}],
        current_user={"role": "user", "content": "first"},
    )
    changed = LLMRequestPlan(
        stable_system="system",
        stable_tools=[{"name": "read"}],
        dynamic_context=[{"role": "user", "content": "memory"}],
        current_user={"role": "user", "content": "second"},
    )

    assert base.diagnostics()["stable_prefix_hash"] == changed.diagnostics()["stable_prefix_hash"]
    assert base.diagnostics()["dynamic_context_hash"] == changed.diagnostics()["dynamic_context_hash"]
