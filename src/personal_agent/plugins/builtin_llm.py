"""Compatibility entrypoint for built-in LLM providers."""

from personal_agent.plugins.builtin.llm import register

__all__ = ["register"]
