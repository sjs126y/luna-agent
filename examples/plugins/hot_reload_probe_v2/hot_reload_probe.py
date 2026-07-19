"""Version two of the manual hot-reload probe plugin."""

from __future__ import annotations

import asyncio
import json

from lumora_plugin_sdk import CommandEntry, ToolEntry


PLUGIN_VERSION = "v2"


def register(ctx) -> None:
    generation_id = ctx.generation_id
    runtime_instance_id = ctx.runtime_instance_id

    def metadata() -> dict[str, str]:
        return {
            "version": PLUGIN_VERSION,
            "generation_id": generation_id,
            "runtime_instance_id": runtime_instance_id,
        }

    async def probe(delay_seconds: int = 0) -> str:
        delay = max(0, min(int(delay_seconds), 20))
        if delay:
            await asyncio.sleep(delay)
        return json.dumps(metadata(), ensure_ascii=False)

    def version_command(args: str = "", **kwargs) -> str:
        return json.dumps(metadata(), ensure_ascii=False)

    ctx.register.tool(ToolEntry(
        name="hot_reload_probe",
        description="Return the active hot-reload probe version and runtime identities.",
        schema={
            "type": "object",
            "properties": {
                "delay_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 20,
                    "default": 0,
                    "description": "Optional delay used to test an in-flight generation switch.",
                },
            },
            "additionalProperties": False,
        },
        handler=probe,
        toolset="runtime",
        tags=["plugin", "hot-reload", "test"],
        idempotent=True,
        is_parallel_safe=True,
    ))
    ctx.register.command(CommandEntry(
        name="hot-version",
        description="Show the active hot-reload probe generation.",
        handler=version_command,
        scope="both",
    ))
