"""Agent dataclass — flat runtime state container. init_agent() does the wiring."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from personal_agent.memory.models import InternalMemorySnapshot

from personal_agent.agent.hooks import Hooks
from personal_agent.agent.retry import RetryState
from personal_agent.llm.provider import ProviderProfile
from personal_agent.tools.registry import tool_registry

TOOL_PROTOCOL_PROMPT = (
    "工具调用规则：\n"
    "- 如果需要读取文件、搜索、执行命令、访问外部状态或使用任何可用工具，必须通过 tool call 调用对应工具。\n"
    "- 不要用文字声称已经调用、读取、搜索或执行了工具，除非本轮实际产生了对应 tool call。\n"
    "- 如果不需要或无法调用工具，请直接说明并回答，不要伪装成已调用工具。"
)


@dataclass
class Agent:
    # ── identity (set by init_agent, never changes) ──
    model: str = ""
    max_iterations: int = 30

    # ── transport & provider ──
    _transport: Any = None                 # BaseTransport instance
    _provider: ProviderProfile | None = None

    # ── tools ──
    tools: list[dict] = field(default_factory=list)
    enabled_toolsets: list[str] | None = None   # None = all tools
    _tools_generation: int = -1

    # ── system prompt ──
    _cached_system_prompt: str | None = None  # None=not built, ""=empty, str=present
    _system_prompt_template: str = ""          # preserved for rebuild after invalidation

    # ── memory ──
    _memory_manager: Any = None
    _memory_session_key: str = ""
    _internal_memory_snapshot: InternalMemorySnapshot | None = None
    _memory_snapshot_turns: int = 0
    _memory_snapshot_refresh_interval: int = 20

    # ── compressor ──
    _compressor: Any = None

    # ── hooks ──
    hooks: Hooks = field(default_factory=Hooks)

    # ── per-session counters (accumulate across turns) ──
    session_prompt_tokens: int = 0
    session_completion_tokens: int = 0
    session_api_calls: int = 0

    # ── per-turn state (reset each build_turn_context) ──
    _iteration_budget: int = 0
    _retry: RetryState = field(default_factory=RetryState)
    _interrupt_requested: bool = False
    _tool_calls_this_turn: int = 0
    _destructive_allowed: set[str] = field(default_factory=set)  # legacy alias for per-turn grants
    _turn_grants: set[str] = field(default_factory=set)
    _temporary_grants: dict[str, float] = field(default_factory=dict)
    _permission_temporary_grant_ttl_seconds: int = 24 * 60 * 60
    _permission_confirm_timeout_seconds: int = 120
    _max_tool_calls_per_turn: int = 20
    _destructive_calls_this_turn: int = 0
    _max_destructive_per_turn: int = 3
    _execution_policy: Any = None
    _pending_skill_injection: str | None = None  # set by Gateway, consumed by context
    _last_skill_injection: str = ""
    _last_skill_summaries: str = ""
    _last_memory_injections: str = ""
    _last_tool_results: list[dict] = field(default_factory=list)

    # ── memory review (Hermes-style background nudge) ──
    _turns_since_memory: int = 0
    _memory_review_interval: int = 10  # nudge every N turns, 0=disabled

    # ── pool split (same pool for MVP, separate later) ──
    _llm_pool: Any = None
    _tool_pool: Any = None


def init_agent(
    transport,
    provider: ProviderProfile,
    *,
    memory_manager=None,
    compressor=None,
    max_iterations: int = 30,
    max_tool_calls_per_turn: int = 20,
    memory_review_interval: int = 10,
    memory_session_key: str = "",
    memory_snapshot_refresh_interval: int = 20,
    system_prompt_template: str = "",
    enabled_toolsets: list[str] | None = None,
    execution_policy=None,
    permission_temporary_grant_ttl_seconds: int = 24 * 60 * 60,
    permission_confirm_timeout_seconds: int = 120,
) -> Agent:
    """Wire an Agent instance. Flat initialization — no 1700-line magic."""
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=8)
    agent = Agent(
        model=provider.model,
        max_iterations=max_iterations,
        _max_tool_calls_per_turn=max_tool_calls_per_turn,
        _memory_review_interval=memory_review_interval,
        _transport=transport,
        _provider=provider,
        _memory_manager=memory_manager,
        _memory_session_key=memory_session_key,
        _memory_snapshot_refresh_interval=memory_snapshot_refresh_interval,
        _compressor=compressor,
        enabled_toolsets=enabled_toolsets,
        _execution_policy=execution_policy,
        _permission_temporary_grant_ttl_seconds=permission_temporary_grant_ttl_seconds,
        _permission_confirm_timeout_seconds=permission_confirm_timeout_seconds,
        _llm_pool=pool,
        _tool_pool=pool,  # shared pool for MVP, separate later
    )
    agent._system_prompt_template = system_prompt_template
    _pin_memory_snapshot(agent)
    _refresh_tools(agent)
    _build_system_prompt(agent, system_prompt_template)
    _register_default_hooks(agent)
    return agent


def _register_default_hooks(agent: Agent) -> None:
    """Non-restrictive default hooks for observability."""
    import logging as _logging
    _log = _logging.getLogger("personal_agent.hooks")

    async def _log_llm_usage(response, usage):
        _log.info("LLM call: in=%d out=%d",
                  usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        return response

    agent.hooks.on_after_llm_call.append(_log_llm_usage)


def _refresh_tools(agent: Agent) -> None:
    """Sync agent.tools with current registry state, respecting enabled_toolsets."""
    gen = tool_registry.generation
    if agent._tools_generation != gen:
        agent.tools = tool_registry.get_definitions(
            enabled_toolsets=agent.enabled_toolsets,
            quiet_mode=True,
        )
        agent._tools_generation = gen
        agent._cached_system_prompt = None  # invalidate


def _build_system_prompt(agent: Agent, template: str = "") -> str:
    """Build or refresh cached system prompt."""
    parts = []
    if template:
        parts.append(template)

    # Tool list (sorted for deterministic byte stream → cache hits)
    if agent.tools:
        tool_lines = [TOOL_PROTOCOL_PROMPT, "可用工具："]
        for t in sorted(agent.tools, key=lambda t: t["name"]):
            tool_lines.append(f"- {t['name']}: {t['description']}")
        parts.append("\n".join(tool_lines))

    # Memory
    if agent._memory_manager:
        snapshot = agent._internal_memory_snapshot
        mem_text = snapshot.content if snapshot is not None else agent._memory_manager.get_system_prompt_text()
        if mem_text:
            parts.append(mem_text)

    agent._cached_system_prompt = "\n\n".join(parts)
    return agent._cached_system_prompt


def _pin_memory_snapshot(agent: Agent) -> None:
    manager = agent._memory_manager
    if manager is None or not hasattr(manager, "get_internal_snapshot"):
        return
    agent._internal_memory_snapshot = manager.get_internal_snapshot(agent._memory_session_key)
    agent._memory_snapshot_turns = 0


def _maybe_refresh_memory_snapshot(agent: Agent) -> bool:
    if agent._internal_memory_snapshot is None:
        return False
    agent._memory_snapshot_turns += 1
    if agent._memory_snapshot_turns < agent._memory_snapshot_refresh_interval:
        return False
    manager = agent._memory_manager
    latest = manager.get_internal_snapshot(agent._memory_session_key)
    agent._memory_snapshot_turns = 0
    if latest.revision == agent._internal_memory_snapshot.revision:
        return False
    agent._internal_memory_snapshot = latest
    agent._cached_system_prompt = None
    return True
