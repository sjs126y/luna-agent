"""Telegram platform plugin entrypoint."""

import importlib


def register(ctx) -> None:
    module = importlib.import_module("personal_agent.adapters.telegram")
    importlib.reload(module)
