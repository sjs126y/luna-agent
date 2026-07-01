"""Compatibility entrypoint for built-in workflows."""

from personal_agent.plugins.builtin.workflows.review import register

__all__ = ["register"]
