"""Prompt wrappers used when Codex events are delivered to Luna."""

from __future__ import annotations

from typing import Any


_COMMON = (
    "你正在协助开发 Luna Agent 插件。\n"
    "插件：{plugin_id}\n"
    "Codex Thread：{thread_id}\n\n"
    "请结合当前插件需求和 LUNA_PLUGIN_DEVELOPMENT.md 处理下面的事件。"
)


def wrap_event(*, plugin_id: str, thread_id: str, event_type: str, text: str) -> str:
    common = _COMMON.format(plugin_id=plugin_id, thread_id=thread_id)
    content = str(text or "").strip()
    templates = {
        "turn_started": (
            "这是 Codex 开始工作的通知。不要因为收到这个事件就询问用户；"
            "只有存在实际需要转达的状态变化时才说明。"
        ),
        "assistant_message": (
            "这是 Codex 的新消息。若答案已经由需求或已确认规范决定，可以直接回复 Codex；"
            "若涉及未确定的架构、能力、权限或数据设计，再询问用户；不要无意义地重复提问。"
        ),
        "progress": (
            "这是开发进度更新。可以记录或合并后告诉用户，不要仅因为普通进度就打断用户。"
        ),
        "request_user_input": (
            "Codex 正在等待输入。结合已确认需求判断是否可以直接回答；不确定时再向用户询问。"
        ),
        "approval_requested": (
            "Codex 请求审批。把具体操作转告用户，等待用户明确决定，不要自行批准。"
        ),
        "turn_completed": (
            "本轮 Codex 交流已经结束。判断是否需要继续回复 Codex，并在必要时向用户简要总结本轮交流。"
        ),
        "error": (
            "Codex 开发出现错误。向用户说明实际错误，不要自动重复相同请求；若可以恢复，说明恢复状态。"
        ),
        "process_restarted": (
            "Codex App Server 曾经退出并被重新启动。向用户说明恢复结果；未完成的 Turn 不要静默继续。"
        ),
    }
    instruction = templates.get(event_type, "这是 Codex 开发事件，请根据上下文处理。")
    return f"{common}\n\n事件类型：{event_type}\n事件内容：\n{content}\n\n处理要求：{instruction}"


def summarize_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event.get("event_id", ""),
        "event_type": event.get("event_type", ""),
        "text": str(event.get("text", ""))[:1000],
        "created_at": event.get("created_at", ""),
        "turn_id": event.get("turn_id", ""),
    }

