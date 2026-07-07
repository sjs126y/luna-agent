"""Shared agent factory behavior."""

from __future__ import annotations

import pytest

from personal_agent.agent.factory import create_agent_runtime
from personal_agent.agent.factory import _resolve_api_mode
from personal_agent.agent.agent import _build_system_prompt, init_agent
from personal_agent.config import Settings
from personal_agent.llm.provider import ProviderProfile, provider_registry
from personal_agent.llm.transport_registry import transport_registry
from personal_agent.models.messages import NormalizedResponse


class FakeTransport:
    def __init__(self, provider):
        self.provider = provider

    async def call(self, messages, system_prompt="", tools=None, max_tokens=4096, stream=False):
        return NormalizedResponse(text="ok", usage={"input_tokens": 1, "output_tokens": 1})


def test_resolve_api_mode_prefers_settings_over_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_MODE", "chat_completions")
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[],
        llm_provider="openai",
        llm_base_url="https://api.ahooqq.cn",
        llm_api_mode="codex_responses",
    )

    assert _resolve_api_mode(settings, "openai") == "codex_responses"


@pytest.mark.asyncio
async def test_create_agent_runtime_resolves_transport_and_compressor(tmp_path):
    transport_registry.register("test_mode", lambda provider: FakeTransport(provider))
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[],
        llm_provider="deepseek",
        llm_base_url="https://example.test",
        llm_api_mode="auto",
        llm_model="deepseek-chat",
        compression_threshold_ratio=0.42,
    )

    original_detect = provider_registry.detect_api_mode
    provider_registry.detect_api_mode = staticmethod(lambda base_url, provider_name: "test_mode")
    try:
        runtime = await create_agent_runtime(settings, system_prompt_template="system")
    finally:
        provider_registry.detect_api_mode = original_detect

    assert isinstance(runtime.transport, FakeTransport)
    assert runtime.agent._compressor is not None
    assert runtime.agent._compressor.threshold_tokens == int(
        runtime.provider.context_window * 0.42
    )
    assert runtime.agent._cached_system_prompt is not None


@pytest.mark.asyncio
async def test_create_agent_runtime_wires_plugin_hooks(tmp_path):
    transport_registry.register("hook_mode", lambda provider: FakeTransport(provider))
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[],
        llm_provider="deepseek",
        llm_base_url="https://example.test",
        llm_api_mode="auto",
    )

    class PluginManager:
        def __init__(self):
            self.calls = []

        async def invoke_hook(self, name, *args, **kwargs):
            self.calls.append(name)
            return None

    manager = PluginManager()
    original_detect = provider_registry.detect_api_mode
    provider_registry.detect_api_mode = staticmethod(lambda base_url, provider_name: "hook_mode")
    try:
        runtime = await create_agent_runtime(settings, plugin_manager=manager)
    finally:
        provider_registry.detect_api_mode = original_detect

    assert "on_agent_created" in manager.calls
    assert runtime.agent.hooks.on_before_llm_call
    assert runtime.agent.hooks.on_after_llm_call
    assert runtime.agent.hooks.on_before_tool_exec
    assert runtime.agent.hooks.on_after_tool_exec


@pytest.mark.asyncio
async def test_create_agent_runtime_supports_codex_responses_mode_from_settings(tmp_path, monkeypatch):
    from personal_agent.plugins.builtin.llm.builtin import register
    from personal_agent.plugins.builtin.llm.builtin.responses import CodexResponsesTransport

    register(None)
    monkeypatch.delenv("LLM_API_MODE", raising=False)
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[],
        llm_provider="openai",
        llm_base_url="https://api.ahooqq.cn",
        llm_api_key="test",
        llm_api_mode="codex_responses",
        llm_model="gpt-5.5",
    )

    runtime = await create_agent_runtime(settings)

    assert isinstance(runtime.transport, CodexResponsesTransport)
    assert runtime.provider.base_url == "https://api.ahooqq.cn"


def test_system_prompt_includes_tool_protocol_before_tool_list():
    provider = ProviderProfile(name="test", base_url="https://example.test", api_key="", model="test-model")
    agent = init_agent(FakeTransport(provider), provider=provider)
    agent.tools = [
        {"name": "web_search", "description": "Search the web"},
        {"name": "read", "description": "Read a file"},
    ]

    prompt = _build_system_prompt(agent, "base system")

    assert "工具调用规则：" in prompt
    assert "必须通过 tool call 调用对应工具" in prompt
    assert "不要用文字声称已经调用" in prompt
    assert prompt.index("工具调用规则：") < prompt.index("可用工具：")
    assert prompt.index("- read: Read a file") < prompt.index("- web_search: Search the web")
