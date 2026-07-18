"""Persistent reminder tools and active scheduler."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from personal_agent.plugins import ActiveResourceRequest, CommandEntry
from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.runtime_context import current_tool_agent


class ActiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    sessions: list[str] = Field(default_factory=list)
    restart_backoff_seconds: list[float] = Field(default_factory=lambda: [1, 2, 5, 10, 30])


class ReminderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: ActiveConfig = Field(default_factory=ActiveConfig)
    max_overdue_seconds: float = Field(default=7 * 24 * 3600, ge=0)
    retry_delay_seconds: float = Field(default=60.0, ge=1.0)
    max_delivery_attempts: int = Field(default=3, ge=1, le=10)


class ReminderRepository:
    def __init__(self, storage) -> None:
        self.storage = storage
        self.changed = asyncio.Event()
        self._lock = asyncio.Lock()
        state = self._read()
        recovered = False
        for reminder in state["reminders"]:
            if reminder.get("status") == "firing":
                reminder["status"] = "scheduled"
                recovered = True
        if recovered:
            self._write(state)

    async def create(self, *, session_key: str, text: str, due_at: datetime) -> dict[str, Any]:
        async with self._lock:
            state = self._read()
            reminder = {
                "reminder_id": f"rem_{uuid4().hex}",
                "session_key": session_key,
                "text": str(text).strip(),
                "due_at": due_at.astimezone(UTC).isoformat(),
                "status": "scheduled",
                "attempts": 0,
                "created_at": datetime.now(UTC).isoformat(),
                "completed_at": "",
                "last_error": "",
            }
            state["reminders"].append(reminder)
            self._write(state)
            self.changed.set()
            return dict(reminder)

    async def list(self, *, session_key: str = "", include_completed: bool = False) -> list[dict[str, Any]]:
        async with self._lock:
            items = self._read()["reminders"]
        result = [
            dict(item)
            for item in items
            if (not session_key or item.get("session_key") == session_key)
            and (include_completed or item.get("status") not in {"completed", "cancelled"})
        ]
        return sorted(result, key=lambda item: (item.get("due_at") or "", item.get("reminder_id") or ""))

    async def cancel(self, reminder_id: str, *, session_key: str = "") -> dict[str, Any] | None:
        async with self._lock:
            state = self._read()
            for item in state["reminders"]:
                if item.get("reminder_id") != reminder_id:
                    continue
                if session_key and item.get("session_key") != session_key:
                    return None
                if item.get("status") in {"completed", "cancelled"}:
                    return dict(item)
                item["status"] = "cancelled"
                item["completed_at"] = datetime.now(UTC).isoformat()
                self._write(state)
                self.changed.set()
                return dict(item)
        return None

    async def due(self, now: datetime) -> list[dict[str, Any]]:
        items = await self.list(include_completed=False)
        return [item for item in items if item.get("status") == "scheduled" and _parse_time(item["due_at"]) <= now]

    async def mark(self, reminder_id: str, status: str, **updates: Any) -> dict[str, Any] | None:
        async with self._lock:
            state = self._read()
            for item in state["reminders"]:
                if item.get("reminder_id") == reminder_id:
                    item["status"] = status
                    item.update(updates)
                    self._write(state)
                    self.changed.set()
                    return dict(item)
        return None

    async def next_due_delay(self, now: datetime) -> float | None:
        items = await self.list(include_completed=False)
        due_times = [_parse_time(item["due_at"]) for item in items if item.get("status") == "scheduled"]
        if not due_times:
            return None
        return max(0.0, min((value - now).total_seconds() for value in due_times))

    def _read(self) -> dict[str, Any]:
        value = self.storage.read_json(
            "reminders.json",
            default={"schema_version": 1, "reminders": []},
            schema_version=1,
        )
        if not isinstance(value.get("reminders"), list):
            raise ValueError("reminder state reminders must be a list")
        return value

    def _write(self, state: dict[str, Any]) -> None:
        self.storage.write_json_atomic("reminders.json", state)


class ReminderRunner:
    def __init__(self, ctx, config: ReminderConfig, repository: ReminderRepository) -> None:
        self.ctx = ctx
        self.config = config
        self.repository = repository

    async def run(self) -> None:
        await self.ctx.runtime.ready()
        while not self.ctx.runtime.stop_requested:
            await self.ctx.runtime.wait_until_resumed()
            self.ctx.runtime.heartbeat()
            await self.fire_due()
            delay = await self.repository.next_due_delay(datetime.now(UTC))
            await self._wait_for_change(delay)

    async def fire_due(self, *, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        for reminder in await self.repository.due(current):
            due_at = _parse_time(reminder["due_at"])
            overdue = max(0.0, (current - due_at).total_seconds())
            if self.config.max_overdue_seconds and overdue > self.config.max_overdue_seconds:
                await self.repository.mark(
                    reminder["reminder_id"],
                    "failed",
                    last_error="reminder exceeded maximum overdue window",
                )
                continue
            attempts = int(reminder.get("attempts") or 0) + 1
            await self.repository.mark(reminder["reminder_id"], "firing", attempts=attempts)
            try:
                handle = await self.ctx.resources.conversation.submit(
                    session_key=reminder["session_key"],
                    text=(
                        "这是一个到期提醒。请结合当前对话和记忆，用自然、简洁的方式提醒用户，"
                        "不要声称执行了提醒之外的操作。\n\n"
                        f"提醒内容：{reminder['text']}\n计划时间：{reminder['due_at']}"
                    ),
                    request_id=f"reminder:{reminder['reminder_id']}",
                    metadata={"plugin": "reminder", "reminder_id": reminder["reminder_id"]},
                )
                outcome_method = getattr(handle, "outcome", None)
                if callable(outcome_method):
                    outcome = await outcome_method()
                    if not bool(getattr(outcome, "succeeded", False)):
                        raise RuntimeError(str(getattr(outcome, "error", "submission failed")))
            except Exception as exc:
                if attempts >= self.config.max_delivery_attempts:
                    status = "failed"
                    next_due = reminder["due_at"]
                else:
                    status = "scheduled"
                    next_due = (current + timedelta(seconds=self.config.retry_delay_seconds)).isoformat()
                await self.repository.mark(
                    reminder["reminder_id"],
                    status,
                    due_at=next_due,
                    last_error=f"{type(exc).__name__}: {exc}",
                )
            else:
                await self.repository.mark(
                    reminder["reminder_id"],
                    "completed",
                    completed_at=datetime.now(UTC).isoformat(),
                    last_error="",
                )

    async def _wait_for_change(self, delay: float | None) -> None:
        timeout = min(max(delay if delay is not None else 3600.0, 0.05), 3600.0)
        self.repository.changed.clear()
        stop_task = asyncio.create_task(self.ctx.runtime.wait_until_stopped())
        change_task = asyncio.create_task(self.repository.changed.wait())
        done, pending = await asyncio.wait(
            {stop_task, change_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def register(ctx) -> None:
    config = ctx.parse_config(ReminderConfig)
    repository_ref: dict[str, ReminderRepository] = {}

    def repository() -> ReminderRepository:
        if "value" not in repository_ref:
            repository_ref["value"] = ReminderRepository(ctx.storage)
        return repository_ref["value"]

    async def create_reminder(text: str, due_at: str) -> str:
        session_key = _current_session_key()
        if not _session_allowed(session_key, config.active.sessions):
            raise PermissionError("reminder session is not allowed by plugin configuration")
        parsed = _parse_time(due_at)
        if not str(text).strip():
            raise ValueError("reminder text must not be empty")
        item = await repository().create(session_key=session_key, text=text, due_at=parsed)
        return json.dumps(item, ensure_ascii=False)

    async def list_reminders(include_completed: bool = False) -> str:
        items = await repository().list(
            session_key=_current_session_key(),
            include_completed=include_completed,
        )
        return json.dumps(items, ensure_ascii=False)

    async def cancel_reminder(reminder_id: str) -> str:
        item = await repository().cancel(reminder_id, session_key=_current_session_key())
        if item is None:
            raise KeyError(f"reminder not found: {reminder_id}")
        return json.dumps(item, ensure_ascii=False)

    ctx.register.tool(ToolEntry(
        name="reminder_create",
        description="Create a persistent reminder for the current conversation session.",
        schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to remind the user about"},
                "due_at": {"type": "string", "description": "ISO 8601 timestamp including timezone"},
            },
            "required": ["text", "due_at"],
        },
        handler=create_reminder,
        toolset="productivity",
        permission_category="default",
        tags=["reminder", "scheduling"],
        idempotent=False,
        is_parallel_safe=False,
    ))
    ctx.register.tool(ToolEntry(
        name="reminder_list",
        description="List reminders belonging to the current conversation session.",
        schema={
            "type": "object",
            "properties": {"include_completed": {"type": "boolean", "default": False}},
        },
        handler=list_reminders,
        toolset="productivity",
        permission_category="read",
        tags=["reminder", "read"],
    ))
    ctx.register.tool(ToolEntry(
        name="reminder_cancel",
        description="Cancel one reminder belonging to the current conversation session.",
        schema={
            "type": "object",
            "properties": {"reminder_id": {"type": "string"}},
            "required": ["reminder_id"],
        },
        handler=cancel_reminder,
        toolset="productivity",
        permission_category="default",
        tags=["reminder", "scheduling"],
        idempotent=True,
        is_parallel_safe=False,
    ))

    async def reminders_command(args="", **kwargs):
        session_key = str(kwargs.get("session_key") or "")
        return _format_reminders(await repository().list(session_key=session_key))

    async def cancel_command(args="", **kwargs):
        reminder_id = str(args or "").strip()
        if not reminder_id:
            return "用法: /remind-cancel <reminder-id>"
        item = await repository().cancel(reminder_id, session_key=str(kwargs.get("session_key") or ""))
        return f"已取消提醒: {reminder_id}" if item else f"未找到提醒: {reminder_id}"

    ctx.register.command(CommandEntry("reminders", "List reminders for this session.", reminders_command, scope="both"))
    ctx.register.command(CommandEntry("remind-cancel", "Cancel a reminder.", cancel_command, scope="both"))

    async def run(active_ctx) -> None:
        await ReminderRunner(active_ctx, config, repository()).run()

    ctx.register.active(
        run=run,
        resources=ActiveResourceRequest(conversation=True),
        restart_policy="on_failure",
        startup_timeout=15,
        shutdown_timeout=15,
    )


def _parse_time(value: str) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("due_at must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("due_at must include a timezone")
    return parsed.astimezone(UTC)


def _current_session_key() -> str:
    agent = current_tool_agent()
    security = getattr(agent, "_security_context", None)
    value = str(getattr(security, "session_key", "") or getattr(agent, "_memory_session_key", "") or "")
    if not value:
        raise RuntimeError("current session is unavailable")
    return value


def _session_allowed(session_key: str, allowed: list[str]) -> bool:
    return bool(session_key and ("*" in allowed or session_key in allowed))


def _format_reminders(items: list[dict[str, Any]]) -> str:
    if not items:
        return "当前会话没有待处理提醒。"
    lines = ["当前提醒:"]
    for item in items:
        lines.append(f"- {item['reminder_id']} {item['due_at']} {item['status']}: {item['text']}")
    return "\n".join(lines)
