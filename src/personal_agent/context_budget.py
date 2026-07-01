"""Unified context budget estimation."""

from __future__ import annotations

from dataclasses import dataclass

from personal_agent.llm.token_counter import count_messages_tokens, count_tools_tokens, estimate_tokens


@dataclass
class ContextBudget:
    system_prompt: int
    history_messages: int
    tools_schema: int
    skills: int
    memory_injections: int
    mcp_tools: int
    remaining_context: int
    context_limit: int

    @property
    def used(self) -> int:
        return (
            self.system_prompt
            + self.history_messages
            + self.tools_schema
            + self.skills
            + self.memory_injections
            + self.mcp_tools
        )

    @property
    def percent(self) -> float:
        return round(self.used / max(self.context_limit, 1) * 100, 1)

    def as_dict(self) -> dict[str, int | float]:
        return {
            "system_prompt": self.system_prompt,
            "history_messages": self.history_messages,
            "tools_schema": self.tools_schema,
            "skills": self.skills,
            "memory_injections": self.memory_injections,
            "mcp_tools": self.mcp_tools,
            "used": self.used,
            "context_limit": self.context_limit,
            "remaining_context": self.remaining_context,
            "percent": self.percent,
        }


def estimate_context_budget(
    *,
    messages: list[dict],
    system_prompt: str = "",
    tools: list[dict] | None = None,
    skills_summary: str = "",
    memory_injections: str = "",
    context_limit: int = 0,
    model: str = "",
) -> ContextBudget:
    if context_limit <= 0:
        from personal_agent.llm.provider import _detect_context_window

        context_limit = _detect_context_window(model)

    all_tools = tools or []
    mcp_tools = [
        tool for tool in all_tools
        if tool.get("name", "").startswith("mcp__")
        or str(tool.get("description", "")).startswith("[MCP ")
    ]
    normal_tools = [tool for tool in all_tools if tool not in mcp_tools]

    system_tokens = estimate_tokens(system_prompt, model)
    history_tokens = count_messages_tokens(messages, model=model)
    tool_tokens = count_tools_tokens(normal_tools, model=model)
    mcp_tokens = count_tools_tokens(mcp_tools, model=model)
    skills_tokens = estimate_tokens(skills_summary, model)
    memory_tokens = estimate_tokens(memory_injections, model)
    used = system_tokens + history_tokens + tool_tokens + mcp_tokens + skills_tokens + memory_tokens

    return ContextBudget(
        system_prompt=system_tokens,
        history_messages=history_tokens,
        tools_schema=tool_tokens,
        skills=skills_tokens,
        memory_injections=memory_tokens,
        mcp_tools=mcp_tokens,
        context_limit=context_limit,
        remaining_context=max(0, context_limit - used),
    )
