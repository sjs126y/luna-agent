"""Date/time utilities."""

from datetime import datetime, timezone, timedelta

from luna_agent.tools.entry import ToolEntry
from luna_agent.tools.registry import tool_registry


async def _datetime(action: str = "now", timezone_offset: int = 8, format: str = "") -> str:
    """Get current time or perform date calculations."""
    tz = timezone(timedelta(hours=timezone_offset))
    now = datetime.now(tz)

    if action == "now":
        fmt = format or "%Y-%m-%d %H:%M:%S %Z"
        return now.strftime(fmt)

    if action == "today":
        return now.strftime("%Y-%m-%d")

    if action == "weekday":
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        return days[now.weekday()]

    if action == "timestamp":
        return str(int(now.timestamp()))

    return f"Unknown action: {action}"


tool_registry.register(ToolEntry(
    name="datetime",
    description="Get current date/time, weekday, or timestamp. Supports timezone offset.",
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["now", "today", "weekday", "timestamp"],
                "description": "What to return. 'now' = full datetime, 'today' = date only, 'weekday' = day name, 'timestamp' = Unix epoch",
            },
            "timezone_offset": {
                "type": "integer",
                "description": "UTC offset in hours (default 8 = Asia/Shanghai)",
            },
            "format": {
                "type": "string",
                "description": "Custom strftime format (only for action='now')",
            },
        },
    },
    handler=_datetime,
    toolset="builtin",
))
