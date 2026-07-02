"""Best-effort background memory review service."""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


DEFAULT_MEMORY_REVIEW_PROMPT = (
    "Review this conversation and save anything worth remembering.\n\n"
    "Focus on:\n"
    "1. Has the user revealed personal details, preferences, or facts worth keeping?\n"
    "2. Has the user expressed expectations about how you should behave?\n\n"
    "If something stands out, call the memory tool to save it. "
    "Use target='user' for preferences, target='memory' for facts.\n"
    "If nothing is worth saving, just reply 'Nothing to save.' and stop."
)


class MemoryReviewService:
    def __init__(
        self,
        *,
        enabled: bool = True,
        prompt: str = DEFAULT_MEMORY_REVIEW_PROMPT,
    ) -> None:
        self.enabled = enabled
        self.prompt = prompt
        self.active = False
        self.cancel_requested = False
        self.spawn_count = 0
        self.saved_count = 0
        self.last_started = ""
        self.last_finished = ""
        self.last_error = ""

    def maybe_spawn(
        self,
        *,
        agent,
        messages: list[dict],
        should_review: bool,
        final_response: str,
    ) -> bool:
        if not self.enabled or not should_review or not final_response or agent is None:
            return False

        import threading

        def _run() -> None:
            import asyncio as _asyncio

            _asyncio.run(self.review(agent=agent, messages=list(messages)))

        self.cancel_requested = False
        self.active = True
        self.spawn_count += 1
        self.last_started = _now()
        thread = threading.Thread(target=_run, daemon=True, name="mem-review")
        thread.start()
        logger.debug("Memory review spawned")
        return True

    async def review(self, *, agent, messages: list[dict]) -> None:
        if self.cancel_requested:
            self.active = False
            self.last_finished = _now()
            return
        self.active = True
        self.last_started = self.last_started or _now()
        try:
            review_messages = list(messages[-12:])
            review_messages.append({
                "role": "user",
                "content": [{"type": "text", "text": self.prompt}],
            })
            if self.cancel_requested:
                return
            response = await agent._transport.call(
                messages=review_messages,
                system_prompt="你是一个记忆管理助手。判断对话中是否有值得保存的信息。",
                tools=getattr(agent, "tools", []),
                max_tokens=512,
            )
            tool_calls = getattr(response, "tool_calls", None)
            if tool_calls and not self.cancel_requested:
                from personal_agent.tools.executor import execute_tool_calls

                await execute_tool_calls(tool_calls, review_messages, agent=agent)
                self.saved_count += len(tool_calls)
                logger.info("Memory review: %d memories saved", len(tool_calls))
            self.last_error = ""
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            self.active = False
            self.last_finished = _now()

    def cancel(self) -> bool:
        was_active = self.active
        self.cancel_requested = True
        return was_active

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "active": self.active,
            "cancel_requested": self.cancel_requested,
            "spawn_count": self.spawn_count,
            "saved_count": self.saved_count,
            "last_started": self.last_started,
            "last_finished": self.last_finished,
            "last_error": self.last_error,
        }

    async def close(self) -> None:
        self.cancel()
        return None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
