"""Compatibility entrypoint for built-in LLM providers."""

from plugins.llm.builtin import register

__all__ = ["register"]
