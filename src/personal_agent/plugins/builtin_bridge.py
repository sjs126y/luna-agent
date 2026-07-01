"""Built-in bridge plugin entrypoint."""

import importlib


def register(ctx) -> None:
    module = importlib.import_module("personal_agent.tools.bridge")
    importlib.reload(module)
