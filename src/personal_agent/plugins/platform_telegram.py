"""Compatibility entrypoint for Telegram platform plugin."""

from personal_agent.plugins.builtin.platforms.telegram import register

__all__ = ["register"]
