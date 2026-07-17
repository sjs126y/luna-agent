"""Async tool confirmation state for Gateway platform messages."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from personal_agent.permissions import (
    confirm_timeout_seconds,
    format_grant_duration,
    temporary_grant_ttl_seconds,
)


@dataclass
class PendingConfirmation:
    session_key: str
    source: Any
    decision: Any
    created_at: float
    expires_at: float
    ttl_seconds: int
    future: asyncio.Future[str] = field(repr=False)

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_key": self.session_key,
            "platform": getattr(self.source, "platform", ""),
            "chat_id": getattr(self.source, "chat_id", ""),
            "user_id": getattr(self.source, "user_id", ""),
            "tool_name": _decision_field(self.decision, "tool_name"),
            "display_name": _decision_field(self.decision, "display_name"),
            "permission_category": _decision_field(self.decision, "permission_category"),
            "batch_items": list(_decision_value(self.decision, "batch_items") or []),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "waiting_seconds": max(0.0, time.time() - self.created_at),
            "temporary_grant_ttl_seconds": self.ttl_seconds,
        }


class PendingConfirmationManager:
    def __init__(self) -> None:
        self._pending: dict[str, PendingConfirmation] = {}

    def get(self, session_key: str) -> PendingConfirmation | None:
        pending = self._pending.get(session_key)
        if pending is None:
            return None
        if pending.future.done():
            self._pending.pop(session_key, None)
            return None
        return pending

    def snapshot(self, session_key: str | None = None) -> dict[str, Any] | None:
        if session_key is not None:
            pending = self.get(session_key)
            return pending.snapshot() if pending is not None else None
        items = [pending.snapshot() for pending in list(self._pending.values()) if not pending.future.done()]
        return {"pending_confirmations": items, "pending_confirmation_count": len(items)}

    async def request(
        self,
        *,
        session_key: str,
        source: Any,
        decision: Any,
        settings: Any,
        send,
    ) -> str:
        if self.get(session_key) is not None:
            return "deny"
        timeout = confirm_timeout_seconds(settings)
        ttl_seconds = temporary_grant_ttl_seconds(settings)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        now = time.time()
        pending = PendingConfirmation(
            session_key=session_key,
            source=source,
            decision=decision,
            created_at=now,
            expires_at=now + timeout,
            ttl_seconds=ttl_seconds,
            future=future,
        )
        self._pending[session_key] = pending
        sent = await send(
            _format_confirmation_prompt(
                decision,
                ttl_seconds=ttl_seconds,
                timeout_seconds=timeout,
            )
        )
        if not sent:
            self._pending.pop(session_key, None)
            return "deny"
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(session_key, None)
            return "deny"
        finally:
            if self._pending.get(session_key) is pending:
                self._pending.pop(session_key, None)

    def resolve_message(self, session_key: str, text: str) -> tuple[bool, str]:
        pending = self.get(session_key)
        if pending is None:
            return False, ""
        answer = _parse_confirmation_answer(text)
        if answer is None:
            return True, "请回复 1、2 或 3；发送 /stop 可取消。"
        if not pending.future.done():
            pending.future.set_result(answer)
        self._pending.pop(session_key, None)
        if answer == "allow":
            return True, "已允许一次，继续执行。"
        if answer == "always":
            duration = format_grant_duration(pending.ttl_seconds)
            return True, f"已允许，{duration}内同类工具不再询问。"
        return True, "已拒绝本次工具调用。"

    def cancel(self, session_key: str | None = None) -> int:
        keys = [session_key] if session_key is not None else list(self._pending)
        count = 0
        for key in keys:
            if key is None:
                continue
            pending = self._pending.pop(key, None)
            if pending is None:
                continue
            if not pending.future.done():
                pending.future.set_result("interrupted")
            count += 1
        return count


def _format_confirmation_prompt(decision: Any, *, ttl_seconds: int, timeout_seconds: int) -> str:
    tool = _decision_field(decision, "display_name") or _decision_field(decision, "tool_name") or "tool"
    category = _decision_field(decision, "permission_category") or "default"
    preview = _decision_field(decision, "input_preview") or _decision_field(decision, "risk_summary")
    batch_items = _decision_value(decision, "batch_items")
    lines = ["需要授权工具调用"]
    if isinstance(batch_items, (list, tuple)) and len(batch_items) > 1:
        lines.append(f"本批次共 {len(batch_items)} 项:")
        for index, item in enumerate(batch_items, start=1):
            item_tool = _decision_field(item, "display_name") or _decision_field(item, "tool_name") or "tool"
            item_preview = _decision_field(item, "input_preview") or _decision_field(item, "risk_summary")
            line = f"{index}. {item_tool}"
            if item_preview:
                line += f" - {item_preview}"
            lines.append(line)
    else:
        lines.extend([f"工具: {tool}", f"权限: {category}"])
        if preview:
            lines.append(f"操作: {preview}")
    duration = format_grant_duration(ttl_seconds)
    lines.extend([
        f"回复 1 允许一次 / 2 拒绝 / 3 {duration}允许",
        f"{timeout_seconds}秒内有效",
    ])
    return "\n".join(lines)


def _parse_confirmation_answer(text: str) -> str | None:
    value = str(text or "").strip().lower()
    mapping = {
        "1": "allow",
        "allow": "allow",
        "允许": "allow",
        "同意": "allow",
        "2": "deny",
        "deny": "deny",
        "拒绝": "deny",
        "不允许": "deny",
        "3": "always",
        "always": "always",
        "始终允许": "always",
        "一直允许": "always",
    }
    return mapping.get(value)


def _decision_field(decision: Any, name: str) -> str:
    return str(_decision_value(decision, name) or "")


def _decision_value(decision: Any, name: str) -> Any:
    if isinstance(decision, dict):
        return decision.get(name)
    return getattr(decision, name, "")
