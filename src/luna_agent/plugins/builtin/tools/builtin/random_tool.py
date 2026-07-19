"""Random utilities — numbers, choices, passwords."""

import random
import secrets
import string

from luna_agent.tools.entry import ToolEntry
from luna_agent.tools.registry import tool_registry


async def _random(action: str = "number", min_val: int = 1, max_val: int = 100,
                  count: int = 1, items: str = "", length: int = 16) -> str:
    try:
        if action == "number":
            if count == 1:
                return str(random.randint(min_val, max_val))
            nums = [str(random.randint(min_val, max_val)) for _ in range(min(count, 100))]
            return ", ".join(nums)

        if action == "choice":
            if not items:
                return "Error: 'items' required for choice action (comma-separated)"
            opts = [i.strip() for i in items.split(",") if i.strip()]
            if not opts:
                return "Error: no items to choose from"
            if count == 1:
                return random.choice(opts)
            picks = random.choices(opts, k=min(count, len(opts)))
            return ", ".join(picks)

        if action == "shuffle":
            if not items:
                return "Error: 'items' required for shuffle action"
            opts = [i.strip() for i in items.split(",") if i.strip()]
            random.shuffle(opts)
            return ", ".join(opts)

        if action == "password":
            chars = string.ascii_letters + string.digits + "!@#$%^&*"
            return "".join(secrets.choice(chars) for _ in range(max(8, min(length, 128))))

        return f"Unknown action: {action}"
    except Exception as e:
        return f"Error: {e}"


tool_registry.register(ToolEntry(
    name="random",
    description="Generate random numbers, pick random choices from a list, shuffle items, or generate secure passwords.",
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["number", "choice", "shuffle", "password"],
                "description": "number=random int, choice=pick from list, shuffle=reorder, password=secure string",
            },
            "min_val": {"type": "integer", "description": "Min value (for number)"},
            "max_val": {"type": "integer", "description": "Max value (for number)"},
            "count": {"type": "integer", "description": "How many results"},
            "items": {"type": "string", "description": "Comma-separated items (for choice/shuffle)"},
            "length": {"type": "integer", "description": "Password length (default 16)"},
        },
        "required": ["action"],
    },
    handler=_random,
    toolset="builtin",
))
