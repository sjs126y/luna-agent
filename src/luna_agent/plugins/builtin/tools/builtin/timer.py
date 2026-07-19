"""Timer/reminder — set a timer, get callback as tool result."""

import asyncio
import time

from luna_agent.tools.entry import ToolEntry
from luna_agent.tools.registry import tool_registry


async def _timer(action: str = "sleep", seconds: int = 5, message: str = "") -> str:
    try:
        if action == "sleep":
            await asyncio.sleep(min(seconds, 300))  # max 5 minutes
            return f"Slept for {seconds}s. " + (message or "Timer done.")

        if action == "timestamp":
            return str(time.time())

        if action == "duration":
            # Return current time, caller can diff
            return f"Current timestamp: {time.time()}"

        return f"Unknown action: {action}"
    except Exception as e:
        return f"Error: {e}"


tool_registry.register(ToolEntry(
    name="timer",
    description="Sleep/wait for a specified duration (max 5 min), get timestamps, or measure durations.",
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["sleep", "timestamp", "duration"],
                "description": "sleep=wait N seconds, timestamp=current epoch, duration=start timing",
            },
            "seconds": {"type": "integer", "description": "Seconds to sleep (default 5, max 300)"},
            "message": {"type": "string", "description": "Message to return after sleep"},
        },
        "required": ["action"],
    },
    handler=_timer,
    toolset="builtin",
    is_parallel_safe=False,  # sequential — sleeping in thread pool is wasteful
))
