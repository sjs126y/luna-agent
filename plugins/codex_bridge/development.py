"""Asynchronous Codex-driven plugin development sessions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import logging
import re
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from luna_agent_plugin_sdk import ActiveConversationIntent

from .app_server import CodexAppServer, CodexAppServerError
from .models import DevelopmentEvent, DevelopmentEventType, DevelopmentSession, DevelopmentStatus, utc_now
from .prompts import wrap_event


_SAFE_ID = re.compile(r"[^a-zA-Z0-9._-]+")
logger = logging.getLogger(__name__)


class DevelopmentStore:
    """Small bounded JSON store; Codex owns the authoritative thread history."""

    def __init__(self, storage, *, max_events: int = 1000) -> None:
        self.storage = storage
        self.max_events = max(100, min(int(max_events), 10000))
        value = storage.read_json("development-sessions.json", default={"schema_version": 1, "sessions": {}})
        self.data = value if isinstance(value, dict) else {"schema_version": 1, "sessions": {}}
        self.data.setdefault("schema_version", 1)
        self.data.setdefault("sessions", {})

    def save(self) -> None:
        self.storage.write_json_atomic("development-sessions.json", self.data)

    def get(self, plugin_id: str) -> DevelopmentSession | None:
        raw = self.data["sessions"].get(plugin_id)
        return DevelopmentSession.from_dict(raw) if isinstance(raw, dict) else None

    def put(self, session: DevelopmentSession) -> None:
        session.updated_at = utc_now()
        session.events = session.events[-self.max_events:]
        self.data["sessions"][session.plugin_id] = session.to_dict()
        self.save()

    def all(self) -> list[DevelopmentSession]:
        return [DevelopmentSession.from_dict(value) for value in self.data["sessions"].values()]


class CodexDevelopmentRuntime:
    def __init__(self, *, config, ctx) -> None:
        self.config = config
        self.ctx = ctx
        self.store = DevelopmentStore(ctx.storage, max_events=config.event_retention)
        self.servers: dict[str, CodexAppServer] = {}
        self.pending_approvals: dict[str, tuple[str, dict[str, Any]]] = {}
        self._queued: dict[str, list[str]] = {}
        self._notified_terminal_turns: set[str] = set()
        self._lock = asyncio.Lock()
        self._active_ctx = None

    async def bind(self, active_ctx) -> None:
        self._active_ctx = active_ctx

    async def run(self, active_ctx) -> None:
        await self.bind(active_ctx)
        await active_ctx.runtime.ready()
        try:
            await active_ctx.runtime.wait_until_stopped()
        finally:
            for server in list(self.servers.values()):
                await server.close()
            self.servers.clear()
            for session in self.store.all():
                if session.status == DevelopmentStatus.RUNNING.value:
                    session.status = DevelopmentStatus.STALE.value
                    session.last_error = "Codex Bridge runtime stopped; active turn was not resumed"
                    self.store.put(session)

    async def create(self, plugin_id: str, description: str, brief: str = "") -> dict[str, Any]:
        plugin_id = self._normalize_id(plugin_id)
        async with self._lock:
            existing = self.store.get(plugin_id)
            if existing:
                return self.summary(existing)
            workspace = await self.ctx.resources.workspace.create(workspace=plugin_id)
            workspace_path = str(_value(workspace, "path") or "")
            if not workspace_path:
                raise RuntimeError("Declared development workspace did not provide a path")
            await self._write_scaffold(plugin_id, description, brief)
            session = DevelopmentSession(
                plugin_id=plugin_id,
                workspace_path=workspace_path,
                brief_path=str(Path(workspace_path) / "PLUGIN_BRIEF.md"),
                spec_revision=self.config.development_spec_revision,
            )
            self.store.put(session)
            return self.summary(session)

    async def message(self, plugin_id: str, text: str) -> dict[str, Any]:
        session = self._require(plugin_id)
        text = str(text or "").strip()
        if not text:
            raise ValueError("text is required")
        first_turn = not session.thread_id
        server = await self._server(session)
        if server.active_turn_id:
            self._queued.setdefault(session.plugin_id, []).append(text)
            session.status = DevelopmentStatus.WAITING_CODEX.value
            self.store.put(session)
            return {"ok": True, "queued": True, "plugin_id": session.plugin_id, "thread_id": session.thread_id}
        turn_id = await server.start_turn(
            _initial_development_prompt(text) if first_turn else text
        )
        session.current_turn_id = turn_id
        session.last_result = ""
        session.last_error = ""
        session.status = DevelopmentStatus.RUNNING.value
        self.store.put(session)
        return {"ok": True, "queued": False, "plugin_id": session.plugin_id, "thread_id": session.thread_id, "turn_id": turn_id}

    async def cancel(self, plugin_id: str) -> dict[str, Any]:
        session = self._require(plugin_id)
        server = self.servers.get(plugin_id)
        if server is not None:
            await server.interrupt()
        self._queued.pop(plugin_id, None)
        session.status = DevelopmentStatus.CANCELLED.value
        self.store.put(session)
        return self.summary(session)

    def list_sessions(self) -> list[dict[str, Any]]:
        return [self.summary(session) for session in self.store.all()]

    def status(self, plugin_id: str) -> dict[str, Any]:
        return self.summary(self._require(plugin_id))

    def events(
        self,
        plugin_id: str,
        limit: int = 20,
        offset: int = 0,
        order: str = "desc",
        event_types: list[str] | None = None,
        detail: str = "summary",
    ) -> dict[str, Any]:
        session = self._require(plugin_id)
        bounded_limit = max(1, min(int(limit), 200))
        bounded_offset = max(0, int(offset))
        normalized_order = str(order or "desc").lower()
        if normalized_order not in {"asc", "desc"}:
            raise ValueError("order must be asc or desc")
        if detail not in {"summary", "full"}:
            raise ValueError("detail must be summary or full")
        allowed_types = {str(value) for value in (event_types or []) if str(value)}
        values = [
            event for event in session.events
            if not allowed_types or str(event.get("event_type")) in allowed_types
        ]
        if normalized_order == "desc":
            values = list(reversed(values))
        page = values[bounded_offset: bounded_offset + bounded_limit]
        if detail == "summary":
            page = [_event_summary(event) for event in page]
        else:
            page = [_event_for_output(event) for event in page]
        next_offset = bounded_offset + len(page)
        return {
            "plugin_id": session.plugin_id,
            "thread_id": session.thread_id,
            "status": session.status,
            "total": len(values),
            "offset": bounded_offset,
            "limit": bounded_limit,
            "returned": len(page),
            "order": normalized_order,
            "detail": detail,
            "event_types": sorted(allowed_types),
            "has_more": next_offset < len(values),
            "next_offset": next_offset if next_offset < len(values) else None,
            "events": page,
        }

    def approvals(self, plugin_id: str = "") -> list[dict[str, Any]]:
        return [
            {"request_id": request_id, "plugin_id": owner, "method": request.get("method", ""), "params": request.get("params", {})}
            for request_id, (owner, request) in self.pending_approvals.items()
            if not plugin_id or owner == plugin_id
        ]

    async def decide_approval(self, request_id: str, decision: str) -> dict[str, Any]:
        entry = self.pending_approvals.get(str(request_id))
        if entry is None:
            raise KeyError(f"approval request not found: {request_id}")
        plugin_id, _ = entry
        server = self.servers.get(plugin_id)
        if server is None:
            raise RuntimeError("Codex App Server is not running")
        decision = str(decision).strip().lower()
        if decision not in {"allow_once", "deny"}:
            raise ValueError("decision must be allow_once or deny")
        await server.respond(request_id, {"decision": "accept" if decision == "allow_once" else "decline"})
        self.pending_approvals.pop(str(request_id), None)
        return {"ok": True, "request_id": request_id, "decision": decision}

    async def _server(self, session: DevelopmentSession) -> CodexAppServer:
        server = self.servers.get(session.plugin_id)
        if server is None or not server.running:
            server = CodexAppServer(
                process_port=self.ctx.resources.process,
                process_name="codex-app-server",
                cwd=Path(session.workspace_path),
                codex_home=self.config.runtime_codex_home,
                approval_policy=self.config.approval_policy,
                approvals_reviewer=self.config.approvals_reviewer,
                sandbox=self.config.sandbox,
                timeout_seconds=self.config.app_server_timeout_seconds,
                on_event=lambda message: self._on_message(session.plugin_id, message),
            )
            self.servers[session.plugin_id] = server
            old_thread = session.thread_id
            try:
                session.thread_id = await server.start(thread_id=old_thread)
            except Exception as exc:
                await server.close()
                self.servers.pop(session.plugin_id, None)
                session.status = DevelopmentStatus.FAILED.value
                session.last_error = str(exc)
                self.store.put(session)
                raise
            session.model = server.effective_model
            session.model_provider = server.effective_model_provider
            self.store.put(session)
            if old_thread:
                await self._record(session, DevelopmentEventType.PROCESS_RESTARTED, "Codex App Server restarted; the previous active turn was not resumed")
        return server

    async def _on_message(self, plugin_id: str, message: dict[str, Any]) -> None:
        session = self.store.get(plugin_id)
        if session is None:
            return
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        approval_request = "requestApproval" in method or "approval" in method.lower()
        if "id" in message and method and approval_request:
            self.pending_approvals[str(message["id"])] = (plugin_id, message)
            event_type = DevelopmentEventType.APPROVAL_REQUESTED
        else:
            event_type = _event_type(method, params)
        if event_type is None:
            return
        text = _event_text(method, params)
        event_turn_id = str(
            params.get("turnId")
            or (params.get("turn") or {}).get("id")
            or session.current_turn_id
        )
        if method == "turn/started":
            session.current_turn_id = event_turn_id
            session.status = DevelopmentStatus.RUNNING.value
        elif event_type == DevelopmentEventType.TURN_COMPLETED:
            session.current_turn_id = ""
            turn = params.get("turn") or {}
            turn_status = str(turn.get("status") or params.get("status") or "").lower()
            turn_error = _event_text(method, params) if "fail" in turn_status else ""
            if "fail" in turn_status:
                session.status = DevelopmentStatus.FAILED.value
                session.last_error = turn_error
            else:
                session.status = DevelopmentStatus.WAITING_CODEX.value
            server = self.servers.get(plugin_id)
            if server is not None:
                server.active_turn_id = ""
            queued = self._queued.get(plugin_id, [])
            if queued:
                next_message = queued.pop(0)
                if not queued:
                    self._queued.pop(plugin_id, None)
                asyncio.create_task(self.message(plugin_id, next_message))
        elif event_type in {DevelopmentEventType.ERROR, DevelopmentEventType.PROCESS_RESTARTED}:
            retrying = event_type == DevelopmentEventType.ERROR and bool(params.get("willRetry"))
            session.status = (
                DevelopmentStatus.RUNNING.value
                if retrying
                else DevelopmentStatus.FAILED.value
                if event_type == DevelopmentEventType.ERROR
                else DevelopmentStatus.STALE.value
            )
            session.last_error = text
        await self._record(
            session,
            event_type,
            text,
            turn_id=event_turn_id,
            metadata={"method": method, "params": _compact(params)},
        )
        notification = self._notification_text(
            session,
            event_type,
            text,
            params,
            turn_id=event_turn_id,
        )
        if notification:
            await self._notify(
                session,
                event_type,
                notification,
                event_key=str(message.get("id") or event_turn_id or uuid4().hex),
            )

    async def _record(self, session, event_type, text, *, turn_id="", metadata=None) -> None:
        event = DevelopmentEvent(
            event_id=f"dev:{uuid4().hex}", plugin_id=session.plugin_id, event_type=event_type.value,
            text=text, thread_id=session.thread_id,
            turn_id=str(turn_id or session.current_turn_id), metadata=metadata or {},
        )
        if event_type == DevelopmentEventType.ASSISTANT_MESSAGE:
            session.last_result = text
        session.events.append(event.to_dict())
        self.store.put(session)

    def _notification_text(self, session, event_type, text, params, *, turn_id: str) -> str:
        """Select only events that require Luna to run a conversation turn."""
        if event_type in {
            DevelopmentEventType.REQUEST_USER_INPUT,
            DevelopmentEventType.APPROVAL_REQUESTED,
        }:
            return text
        if event_type == DevelopmentEventType.PROCESS_RESTARTED:
            return text if session.current_turn_id else ""
        if event_type == DevelopmentEventType.ERROR:
            if bool(params.get("willRetry")):
                return ""
            return text if self._claim_terminal_turn(session.plugin_id, turn_id) else ""
        if event_type == DevelopmentEventType.TURN_COMPLETED:
            turn = params.get("turn") or {}
            status = str(turn.get("status") or params.get("status") or "").lower()
            if "fail" in status:
                return text if self._claim_terminal_turn(session.plugin_id, turn_id) else ""
            return str(session.last_result or text).strip()
        return ""

    def _claim_terminal_turn(self, plugin_id: str, turn_id: str) -> bool:
        key = f"{plugin_id}:{turn_id or 'unknown'}"
        if key in self._notified_terminal_turns:
            return False
        if len(self._notified_terminal_turns) >= 1000:
            self._notified_terminal_turns.clear()
        self._notified_terminal_turns.add(key)
        return True

    async def _notify(self, session, event_type, text, *, event_key: str) -> None:
        if self._active_ctx is None:
            return
        sessions = set(self.config.active.sessions or [])
        if not sessions:
            return
        instruction = wrap_event(plugin_id=session.plugin_id, thread_id=session.thread_id, event_type=event_type.value, text=text)
        for session_key in sessions:
            try:
                target_key = uuid5(NAMESPACE_URL, session_key).hex
                await self._active_ctx.resources.conversation.submit_intent(ActiveConversationIntent(
                    intent_id=f"codex:{session.plugin_id}:{event_type.value}:{event_key}:{target_key}", session_key=session_key,
                    kind=f"codex_{event_type.value}", instruction=instruction,
                    evidence={"plugin_id": session.plugin_id, "thread_id": session.thread_id, "event_type": event_type.value},
                    request_id=f"codex-event:{session.plugin_id}:{event_type.value}:{event_key}:{target_key}",
                ))
            except Exception as exc:
                logger.warning(
                    "Codex event delivery failed: plugin=%s session=%s event=%s error=%s",
                    session.plugin_id,
                    session_key,
                    event_type.value,
                    exc,
                )

    def _require(self, plugin_id: str) -> DevelopmentSession:
        plugin_id = self._normalize_id(plugin_id)
        session = self.store.get(plugin_id)
        if session is None:
            raise KeyError(f"development session not found: {plugin_id}")
        return session

    @staticmethod
    def _normalize_id(value: str) -> str:
        result = _SAFE_ID.sub("-", str(value or "").strip()).strip("-.")
        if not result or result in {".", ".."}:
            raise ValueError("plugin_id must contain letters, numbers, '.', '_' or '-'")
        return result[:80]

    def summary(self, session: DevelopmentSession) -> dict[str, Any]:
        return {**session.to_dict(), "events": len(session.events), "server_running": bool(self.servers.get(session.plugin_id) and self.servers[session.plugin_id].running)}

    async def _write_scaffold(self, plugin_id: str, description: str, brief: str) -> None:
        workspace = self.ctx.resources.workspace
        await workspace.write(
            path=f"{plugin_id}/.codex-plugin/plugin.json",
            text=json.dumps({"name": plugin_id, "version": "0.1.0", "description": description}, indent=2) + "\n",
        )
        await workspace.write(
            path=f"{plugin_id}/plugin.yaml",
            text=f"schema_version: 1\nkey: external/{plugin_id}\nname: {plugin_id}\nversion: \"0.1.0\"\nplugin_api: \">=1,<2\"\nentrypoint: plugin:register\nprovides: [tool]\n",
        )
        await workspace.write(
            path=f"{plugin_id}/PLUGIN_BRIEF.md",
            text=f"# {plugin_id}\n\n{description}\n\n{brief}\n",
        )
        if self.config.development_spec_path:
            await workspace.copy(
                source=str(self.config.development_spec_path),
                path=f"{plugin_id}/LUNA_PLUGIN_DEVELOPMENT.md",
            )


def _value(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _event_type(method: str, params: dict[str, Any] | None = None) -> DevelopmentEventType | None:
    params = params or {}
    if method in {"turn/started", "turn.started"}:
        return DevelopmentEventType.TURN_STARTED
    if method == "item/agentMessage/completed":
        return DevelopmentEventType.ASSISTANT_MESSAGE
    if method == "item/completed":
        item_type = str((params.get("item") or {}).get("type") or "")
        if item_type == "userMessage":
            return None
        if item_type == "agentMessage":
            return DevelopmentEventType.ASSISTANT_MESSAGE
        return DevelopmentEventType.PROGRESS
    if method == "item/agentMessage/delta":
        # Deltas are intentionally not submitted as separate active intents.
        return None
    if "requestUserInput" in method or "request_user_input" in method:
        return DevelopmentEventType.REQUEST_USER_INPUT
    if "requestApproval" in method or "approval" in method.lower():
        return DevelopmentEventType.APPROVAL_REQUESTED
    if method in {"turn/completed", "turn.completed"}:
        return DevelopmentEventType.TURN_COMPLETED
    if method in {"codex/processError", "error"}:
        return DevelopmentEventType.ERROR
    if method in {"codex/processExited", "codex/processRestarted"}:
        return DevelopmentEventType.PROCESS_RESTARTED
    if method:
        return DevelopmentEventType.PROGRESS
    return None


def _event_text(method: str, params: dict[str, Any]) -> str:
    for key in ("text", "delta", "message", "error", "reason"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = _dict_text(value)
            if nested:
                return nested
    item = params.get("item")
    if isinstance(item, dict):
        for key in ("text", "content", "summary", "command", "aggregatedOutput", "output"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list):
                texts = [
                    str(part.get("text") or "").strip()
                    for part in value
                    if isinstance(part, dict) and str(part.get("text") or "").strip()
                ]
                if texts:
                    return "\n".join(texts)
        item_type = str(item.get("type") or "").strip()
        status = str(item.get("status") or "").strip()
        if item_type:
            return f"{item_type}{f': {status}' if status else ''}"
    turn = params.get("turn")
    if isinstance(turn, dict):
        error = turn.get("error")
        if isinstance(error, dict):
            nested = _dict_text(error)
            if nested:
                return nested
        status = str(turn.get("status") or "").strip()
        if status:
            return f"turn {status}"
    return method or "Codex event"


def _compact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _compact(v) for k, v in list(value.items())[:20]}
    if isinstance(value, list):
        return [_compact(v) for v in value[:20]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value if not isinstance(value, str) else value[:1000]
    return str(value)


def _dict_text(value: dict[str, Any]) -> str:
    parts = []
    for key in ("message", "additionalDetails", "reason", "detail"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            parts.append(item.strip())
    return " - ".join(dict.fromkeys(parts))


def _event_summary(event: dict[str, Any]) -> dict[str, Any]:
    output = _event_for_output(event)
    metadata = output.get("metadata") or {}
    return {
        "event_id": output.get("event_id", ""),
        "event_type": output.get("event_type", ""),
        "text": str(output.get("text", ""))[:2000],
        "created_at": output.get("created_at", ""),
        "created_at_utc": output.get("created_at_utc", ""),
        "turn_id": output.get("turn_id", ""),
        "method": metadata.get("method", "") if isinstance(metadata, dict) else "",
    }


def _event_for_output(event: dict[str, Any]) -> dict[str, Any]:
    """Expose local time while retaining UTC as the canonical audit value."""
    output = dict(event)
    raw_timestamp = str(event.get("created_at", "") or "")
    output["created_at_utc"] = raw_timestamp
    output["created_at"] = _local_timestamp(raw_timestamp)
    return output


def _local_timestamp(value: str) -> str:
    if not value:
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone().isoformat()
    except (TypeError, ValueError):
        return value


def _initial_development_prompt(feature_request: str) -> str:
    return (
        "You are developing a Luna Agent plugin in an isolated plugin workspace.\n"
        "Before editing anything, read LUNA_PLUGIN_DEVELOPMENT.md and PLUGIN_BRIEF.md "
        "from the current directory. Those files are the authoritative engineering, "
        "security, package, SDK, testing, and lifecycle contract. Do not ask the user to "
        "repeat routine implementation details already covered there. Use sound engineering "
        "judgment, implement the requested feature, and run the relevant validation and tests. "
        "Do not install the plugin into the host application.\n\n"
        "User's feature requirement:\n"
        f"{feature_request.strip()}"
    )
