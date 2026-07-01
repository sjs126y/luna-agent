"""WeChat platform plugin entrypoint."""

import importlib


def register(ctx) -> None:
    module = importlib.import_module("personal_agent.adapters.wechat")
    importlib.reload(module)
