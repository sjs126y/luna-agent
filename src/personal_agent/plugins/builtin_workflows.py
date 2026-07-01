"""Built-in workflow plugin entrypoint."""

import importlib


def register(ctx) -> None:
    module = importlib.import_module("personal_agent.workflow.builtin.review")
    importlib.reload(module)
