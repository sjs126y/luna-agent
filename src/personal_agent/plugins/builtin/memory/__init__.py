"""Built-in memory plugin entrypoint."""

import importlib


def register(ctx) -> None:
    from personal_agent.tools.registry import tool_registry

    module = importlib.import_module("personal_agent.plugins.builtin.memory.file.provider")
    missing = [name for name in ("memory", "memory_ingest") if tool_registry.get(name) is None]
    if missing:
        importlib.reload(module)

    for name in ("memory", "memory_ingest"):
        entry = tool_registry.get(name)
        if entry is None:
            raise RuntimeError(f"Memory tool did not register: {name}")
        ctx.register_tool(entry)
