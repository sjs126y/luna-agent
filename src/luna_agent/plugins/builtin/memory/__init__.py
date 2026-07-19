"""Built-in memory tool registration; storage lives behind MemoryManager."""


def register(ctx) -> None:
    from luna_agent.memory.tools import memory_buffer_tool_entry, memory_tool_entry

    ctx.register.tool(memory_tool_entry())
    ctx.register.tool(memory_buffer_tool_entry())
