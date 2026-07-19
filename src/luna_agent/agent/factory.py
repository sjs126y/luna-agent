"""Shared agent runtime factory for Gateway and CLI entrypoints."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from luna_agent.agent.agent import init_agent
from luna_agent.compression import compression_registry
from luna_agent.llm.provider import ProviderProfile, provider_registry
from luna_agent.llm.transport_registry import transport_registry

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
    session_key: str = "",
    capability_view=None,
) -> AgentRuntime:
    """Resolve provider/transport/compressor and assemble an Agent."""
    provider_name = settings.llm_provider
    provider = provider_registry.get(provider_name, settings)
    if provider_name == "openrouter":
        from luna_agent.llm.model_metadata import enrich_openrouter_profile

        await enrich_openrouter_profile(provider, settings.agent_data_dir)
    api_mode = _resolve_api_mode(settings, provider_name, provider)
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
        memory_session_key=session_key,
        memory_snapshot_refresh_interval=getattr(settings, "memory_snapshot_refresh_turn_interval", 20),
        system_prompt_template=system_prompt_template,
        enabled_toolsets=settings.enabled_toolsets,
        hook_manager=getattr(plugin_manager, "hook_manager", None),
        plugin_manager=plugin_manager,
        capability_view=capability_view,
        approval_reviewer_config=getattr(settings, "approval_reviewer_config", {}),
    )

    if plugin_manager is not None:
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

    from luna_agent.workflow.engine import setup_engine

    setup_engine(
        call_fn=transport.call,
        tools=agent.tools,
        max_tokens=provider.max_tokens,
    )

    return AgentRuntime(agent=agent, provider=provider, transport=transport)


def _resolve_api_mode(
    settings,
    provider_name: str,
    provider: ProviderProfile | None = None,
) -> str:
    if provider is not None and provider.api_mode:
        return provider.api_mode
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
