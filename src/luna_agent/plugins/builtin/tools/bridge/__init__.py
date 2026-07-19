"""Tool bridge plugin entrypoint."""

import importlib


def register(ctx) -> None:
    from luna_agent.tools.registry import tool_registry

    module = importlib.import_module("luna_agent.plugins.builtin.tools.bridge.bridge")
    missing = [name for name in ("tool_search", "tool_describe", "tool_call") if tool_registry.get(name) is None]
    if missing:
        importlib.reload(module)

    for name in ("tool_search", "tool_describe", "tool_call"):
        entry = tool_registry.get(name)
        if entry is None:
            raise RuntimeError(f"Bridge tool did not register: {name}")
        ctx.register.tool(entry)
