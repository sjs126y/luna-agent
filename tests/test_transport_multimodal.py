from __future__ import annotations

from personal_agent.llm.provider import ProviderProfile, provider_registry
from personal_agent.plugins.builtin.llm.builtin.anthropic import AnthropicMessagesTransport
from personal_agent.plugins.builtin.llm.builtin.chat_completions import ChatCompletionsTransport


def test_chat_completions_preserves_image_url_content():
    provider = ProviderProfile(
        name="openai",
        base_url="https://example.test",
        api_key="k",
        model="gpt-4o",
        supports_image_input=True,
    )
    transport = ChatCompletionsTransport(provider)

    converted = transport.convert_messages([
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }
    ])

    assert converted == [{
        "role": "user",
        "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }]


def test_anthropic_converts_data_url_to_image_source():
    provider = ProviderProfile(
        name="anthropic",
        base_url="https://example.test",
        api_key="k",
        model="claude",
        supports_image_input=True,
    )
    transport = AnthropicMessagesTransport(provider)

    converted = transport.convert_messages([
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }
    ])

    assert converted[0]["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
    }


def test_builtin_provider_multimodal_capabilities_are_conservative():
    settings = type("Settings", (), {
        "llm_base_url": "https://example.test",
        "llm_api_key": "k",
        "llm_model": "deepseek-chat",
        "llm_max_tokens": 4096,
    })()

    assert provider_registry.get("deepseek", settings).supports_image_input is False
    assert provider_registry.get("openrouter", settings).supports_image_input is False
    assert provider_registry.get("openai", settings).supports_image_input is True
    assert provider_registry.get("anthropic", settings).supports_image_input is True
