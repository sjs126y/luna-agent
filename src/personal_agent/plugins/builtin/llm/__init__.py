"""Built-in LLM provider plugin entrypoint."""

import importlib


def register(ctx) -> None:
    module = importlib.import_module("personal_agent.llm")
    importlib.reload(module)

