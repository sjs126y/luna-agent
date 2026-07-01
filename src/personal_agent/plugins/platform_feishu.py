"""Feishu platform plugin entrypoint."""

import importlib


def register(ctx) -> None:
    module = importlib.import_module("personal_agent.adapters.feishu")
    importlib.reload(module)
