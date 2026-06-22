"""SimpleCompressor: summarize old messages using the current model.

NOTE: Each compression = one extra LLM API call.
"""

from __future__ import annotations

import logging

from personal_agent.compression.base import Compressor

logger = logging.getLogger(__name__)


class SimpleCompressor(Compressor):
    """Uses the current model for summarization (no separate compressor model)."""

    async def compress(
        self,
        messages: list[dict],
        system_prompt: str,
        transport,
        protect_head: int = 2,
        protect_tail: int = 6,
    ) -> list[dict]:
        if len(messages) <= protect_head + protect_tail + 2:
            return messages

        head = messages[:protect_head]
        middle = messages[protect_head:-protect_tail]
        tail = messages[-protect_tail:]

        # Build summary prompt
        summary_input = _format_messages_for_summary(middle)
        compress_request = [
            {"role": "user", "content": [{"type": "text", "text": (
                "请将以下对话历史压缩为一段简洁的摘要，保留关键信息和上下文。"
                "用中文回复，不超过300字。\n\n"
                f"{summary_input}"
            )}]},
        ]

        try:
            resp = await transport.call(
                messages=compress_request,
                system_prompt="你是一个摘要助手。请简洁地总结对话。",
                max_tokens=500,
            )
            summary = resp.text.strip()
        except Exception:
            logger.exception("Compression LLM call failed, truncating")
            return head + tail

        if not summary:
            return head + tail

        # Inject summary as synthetic system message
        summary_msg = {
            "role": "user",
            "content": [{"type": "text", "text": f"[历史对话摘要]\n{summary}"}],
        }
        return head + [summary_msg] + tail


def _format_messages_for_summary(messages: list[dict]) -> str:
    lines = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            texts = []
            for block in content:
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    texts.append(f"[调用工具: {block.get('name')}]")
                elif block.get("type") == "tool_result":
                    result = str(block.get("content", ""))[:200]
                    texts.append(f"[工具结果: {result}]")
            text = " ".join(texts)
        else:
            text = str(content)
        lines.append(f"{role}: {text[:300]}")
    return "\n".join(lines)
