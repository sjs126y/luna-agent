"""Shared agent runtime factory for Gateway and CLI entrypoints."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from personal_agent.agent.agent import init_agent
from personal_agent.compression import compression_registry
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
    api_mode = _resolve_api_mode(settings, provider_name)
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
        execution_policy=getattr(settings, "execution_policy", None),
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


def _resolve_api_mode(settings, provider_name: str) -> str:
    configured = str(getattr(settings, "llm_api_mode", "auto") or "auto").strip()
    if configured and configured != "auto":
        return configured
    return provider_registry.detect_api_mode(settings.llm_base_url, provider_name)


def _create_compressor(settings, provider: ProviderProfile, api_mode: str):
    engine_name = str(getattr(settings, "compressor_engine", "compressor") or "").strip().lower()
    if engine_name in {"", "none", "off", "disabled"}:
        return None

    try:
        factory = compression_registry.get(engine_name)
    except KeyError:
        logger.warning("Unknown compression engine: %s", engine_name)
        return None

    return factory(settings, provider, api_mode)


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
