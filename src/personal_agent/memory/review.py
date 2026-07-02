"""Best-effort background memory review service."""

from __future__ import annotations

import logging

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

        thread = threading.Thread(target=_run, daemon=True, name="mem-review")
        thread.start()
        logger.debug("Memory review spawned")
        return True

    async def review(self, *, agent, messages: list[dict]) -> None:
        try:
            review_messages = list(messages[-12:])
            review_messages.append({
                "role": "user",
                "content": [{"type": "text", "text": self.prompt}],
            })
            response = await agent._transport.call(
                messages=review_messages,
                system_prompt="你是一个记忆管理助手。判断对话中是否有值得保存的信息。",
                tools=getattr(agent, "tools", []),
                max_tokens=512,
            )
            tool_calls = getattr(response, "tool_calls", None)
            if tool_calls:
                from personal_agent.tools.executor import execute_tool_calls

                await execute_tool_calls(tool_calls, review_messages, agent=agent)
                logger.info("Memory review: %d memories saved", len(tool_calls))
        except Exception:
            pass

    async def close(self) -> None:
        return None
