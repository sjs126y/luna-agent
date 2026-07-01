"""Shared agent runtime factory for Gateway and CLI entrypoints."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from personal_agent.agent.agent import init_agent
from personal_agent.compression.simple import ContextCompressor
from personal_agent.llm.provider import ProviderProfile, provider_registry
from personal_agent.llm.transport_registry import transport_registry

logger = logging.getLogger(__name__)


@dataclass
class AgentRuntime:
    agent: Any
    provider: ProviderProfile
    transport: Any


async def create_agent_runtime(
    settings,
    *,
    memory_manager=None,
    plugin_manager=None,
    system_prompt_template: str = "",
) -> AgentRuntime:
    """Resolve provider/transport/compressor and assemble an Agent."""
    provider_name = settings.llm_provider
    provider = provider_registry.get(provider_name, settings)
    api_mode = provider_registry.detect_api_mode(settings.llm_base_url, provider_name)
    transport = transport_registry.get(api_mode, provider)
    logger.debug("Agent transport: provider=%s api_mode=%s", provider_name, api_mode)

    compressor = _create_compressor(settings, provider, api_mode)
    agent = init_agent(
        transport,
        provider,
        memory_manager=memory_manager,
        compressor=compressor,
        max_iterations=settings.max_iterations,
        max_tool_calls_per_turn=settings.max_tool_calls_per_turn,
        memory_review_interval=settings.memory_review_interval,
        system_prompt_template=system_prompt_template,
        enabled_toolsets=settings.enabled_toolsets,
    )

    if plugin_manager is not None:
        _wire_plugin_hooks(agent, plugin_manager)
        await plugin_manager.invoke_hook(
            "on_agent_created",
            agent=agent,
            transport=transport,
            provider=provider,
            call_fn=transport.call,
            tools=agent.tools,
            max_tokens=provider.max_tokens,
            settings=settings,
        )

    from personal_agent.workflow.engine import setup_engine

    setup_engine(
        call_fn=transport.call,
        tools=agent.tools,
        max_tokens=provider.max_tokens,
    )

    return AgentRuntime(agent=agent, provider=provider, transport=transport)


def _create_compressor(settings, provider: ProviderProfile, api_mode: str):
    if settings.compressor_engine not in ("simple", "compressor"):
        return None

    compressor_transport = None
    if settings.compressor_model:
        comp_provider = ProviderProfile(
            name="compressor",
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.compressor_model,
            max_tokens=512,
        )
        compressor_transport = transport_registry.get(api_mode, comp_provider)

    return ContextCompressor(
        context_length=provider.context_window or 64_000,
        threshold_ratio=settings.compression_threshold_ratio,
        tail_token_budget=settings.tail_token_budget,
        max_summary_tokens=settings.compressor_max_tokens,
        compressor_transport=compressor_transport,
        model=provider.model or "",
    )


def _wire_plugin_hooks(agent, plugin_manager) -> None:
    async def _before_llm(messages, system_prompt, tools):
        return await plugin_manager.invoke_hook(
            "on_before_llm_call", messages, system_prompt, tools
        )

    async def _after_llm(response, usage):
        return await plugin_manager.invoke_hook("on_after_llm_call", response, usage)

    async def _before_tool(tool_call, agent_obj):
        return await plugin_manager.invoke_hook("on_before_tool_exec", tool_call, agent_obj)

    async def _after_tool(tool_call, result):
        return await plugin_manager.invoke_hook("on_after_tool_exec", tool_call, result)

    agent.hooks.on_before_llm_call.append(_before_llm)
    agent.hooks.on_after_llm_call.append(_after_llm)
    agent.hooks.on_before_tool_exec.append(_before_tool)
    agent.hooks.on_after_tool_exec.append(_after_tool)
