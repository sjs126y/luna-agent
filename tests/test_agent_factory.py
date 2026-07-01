"""Shared agent factory behavior."""

from __future__ import annotations

import pytest

from personal_agent.agent.factory import create_agent_runtime
from personal_agent.config import Settings
from personal_agent.llm.provider import provider_registry
from personal_agent.llm.transport_registry import transport_registry
from personal_agent.models.messages import NormalizedResponse


class FakeTransport:
    def __init__(self, provider):
        self.provider = provider

    async def call(self, messages, system_prompt="", tools=None, max_tokens=4096, stream=False):
        return NormalizedResponse(text="ok", usage={"input_tokens": 1, "output_tokens": 1})


@pytest.mark.asyncio
async def test_create_agent_runtime_resolves_transport_and_compressor(tmp_path):
    transport_registry.register("test_mode", lambda provider: FakeTransport(provider))
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[],
        llm_provider="deepseek",
        llm_base_url="https://example.test",
        llm_api_mode="test_mode",
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
