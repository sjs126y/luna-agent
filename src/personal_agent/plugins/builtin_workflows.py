"""Compatibility entrypoint for built-in workflows."""

from plugins.workflows.review import register

__all__ = ["register"]
