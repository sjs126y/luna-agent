"""Compatibility entrypoint for Telegram platform plugin."""

from plugins.platforms.telegram import register

__all__ = ["register"]
