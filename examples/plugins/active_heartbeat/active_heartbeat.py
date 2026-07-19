"""Minimal active plugin that records a heartbeat in isolated storage."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from luna_agent_plugin_sdk import ActiveResourceRequest


async def run(ctx) -> None:
    interval = max(1.0, float(ctx.config.get("interval_seconds", 30)))
    storage = ctx.resources.storage
    storage.write_text("status.txt", "starting\n")
    await ctx.runtime.ready()
    while not ctx.runtime.stop_requested:
        await ctx.runtime.wait_until_resumed()
        ctx.runtime.heartbeat()
        storage.write_text("status.txt", datetime.now(UTC).isoformat() + "\n")
        try:
            await asyncio.wait_for(ctx.runtime.wait_until_stopped(), timeout=interval)
        except asyncio.TimeoutError:
            pass


def register(ctx) -> None:
    ctx.register.active(
        run=run,
        resources=ActiveResourceRequest(),
        restart_policy="on_failure",
        startup_timeout=10,
        shutdown_timeout=10,
    )
