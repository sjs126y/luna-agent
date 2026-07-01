"""Weather via wttr.in — free, no API key needed."""

import httpx
from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry


async def _weather(city: str, format: str = "3") -> str:
    """Get weather from wttr.in. format: 1-4 (1=cond, 2=cond+wind, 3=full, 4=v2)."""
    try:
        url = f"https://wttr.in/{city}?format={format}&lang=zh"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers={"User-Agent": "curl"})
            resp.raise_for_status()
        return resp.text.strip()
    except Exception as e:
        return f"Error fetching weather: {e}"


tool_registry.register(ToolEntry(
    name="weather",
    description="Get current weather for a city. Uses wttr.in (free, no API key). City can be name or pinyin (e.g., 'Beijing', 'guangzhou').",
    schema={
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "City name or pinyin, e.g. 'Beijing', 'shanghai'"},
            "format": {"type": "string", "description": "Output detail: 1=condition only, 2=+wind, 3=full (default), 4=v2"},
        },
        "required": ["city"],
    },
    handler=_weather,
    toolset="builtin",
))
