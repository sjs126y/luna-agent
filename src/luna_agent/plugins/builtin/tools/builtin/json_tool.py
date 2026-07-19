"""JSON/YAML utilities — parse, format, validate."""

import json
from luna_agent.tools.entry import ToolEntry
from luna_agent.tools.registry import tool_registry


async def _json_tool(action: str = "format", input: str = "", query: str = "",
                      indent: int = 2) -> str:
    try:
        if action == "format":
            try:
                data = json.loads(input)
                return json.dumps(data, indent=indent, ensure_ascii=False)
            except json.JSONDecodeError as e:
                return f"Error: invalid JSON — {e}"

        if action == "validate":
            try:
                json.loads(input)
                return "Valid JSON"
            except json.JSONDecodeError as e:
                return f"Invalid JSON: {e}"

        if action == "keys":
            try:
                data = json.loads(input)
                if isinstance(data, dict):
                    return ", ".join(data.keys())
                if isinstance(data, list):
                    return f"Array with {len(data)} items"
                return f"Scalar value: {data}"
            except json.JSONDecodeError as e:
                return f"Error: {e}"

        if action == "get":
            try:
                data = json.loads(input)
                parts = query.split(".")
                for p in parts:
                    if isinstance(data, dict):
                        data = data[p]
                    elif isinstance(data, list):
                        data = data[int(p)]
                return json.dumps(data, indent=indent, ensure_ascii=False)
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
                return f"Error: {e}"

        return f"Unknown action: {action}"
    except Exception as e:
        return f"Error: {e}"


tool_registry.register(ToolEntry(
    name="json",
    description="Parse, format, validate, or query JSON data. Use 'get' with dot-notation to extract nested fields (e.g., 'data.users.0.name').",
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["format", "validate", "keys", "get"],
                "description": "format=pretty-print, validate=check JSON, keys=list top-level keys, get=extract by path",
            },
            "input": {"type": "string", "description": "JSON string to operate on"},
            "query": {"type": "string", "description": "Dot-notation path for 'get' action, e.g. 'users.0.name'"},
            "indent": {"type": "integer", "description": "Indentation for formatted output (default 2)"},
        },
        "required": ["action", "input"],
    },
    handler=_json_tool,
    toolset="builtin",
))
