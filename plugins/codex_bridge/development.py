"""Asynchronous Codex-driven plugin development sessions."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from luna_agent_plugin_sdk import ActiveConversationIntent

from .app_server import CodexAppServer, CodexAppServerError
from .models import DevelopmentEvent, DevelopmentEventType, DevelopmentSession, DevelopmentStatus, utc_now
from .prompts import wrap_event


_SAFE_ID = re.compile(r"[^a-zA-Z0-9._-]+")


class DevelopmentStore:
    """Small bounded JSON store; Codex owns the authoritative thread history."""

    def __init__(self, storage) -> None:
        self.storage = storage
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
        session.events = session.events[-50:]
        self.data["sessions"][session.plugin_id] = session.to_dict()
        self.save()

    def all(self) -> list[DevelopmentSession]:
        return [DevelopmentSession.from_dict(value) for value in self.data["sessions"].values()]


class CodexDevelopmentRuntime:
    def __init__(self, *, config, ctx) -> None:
        self.config = config
        self.ctx = ctx
        self.store = DevelopmentStore(ctx.storage)
        self.servers: dict[str, CodexAppServer] = {}
        self.pending_approvals: dict[str, tuple[str, dict[str, Any]]] = {}
        self._queued: dict[str, list[str]] = {}
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
                if session.status in {DevelopmentStatus.RUNNING.value, DevelopmentStatus.WAITING_CODEX.value}:
                    session.status = DevelopmentStatus.STALE.value
                    session.last_error = "Codex Bridge runtime stopped; active turn was not resumed"
                    self.store.put(session)

    async def create(self, plugin_id: str, description: str, brief: str = "") -> dict[str, Any]:
        plugin_id = self._normalize_id(plugin_id)
        async with self._lock:
            existing = self.store.get(plugin_id)
            if existing:
                return self.summary(existing)
            workspace = self.config.development_root / plugin_id
            workspace.mkdir(parents=True, exist_ok=True)
            self._write_scaffold(workspace, plugin_id, description, brief)
            session = DevelopmentSession(
                plugin_id=plugin_id,
                workspace_path=str(workspace),
                brief_path=str(workspace / "PLUGIN_BRIEF.md"),
                spec_revision=self.config.development_spec_revision,
            )
            self.store.put(session)
            return self.summary(session)

    async def message(self, plugin_id: str, text: str) -> dict[str, Any]:
        session = self._require(plugin_id)
        text = str(text or "").strip()
        if not text:
            raise ValueError("text is required")
        server = await self._server(session)
        if server.active_turn_id:
            self._queued.setdefault(session.plugin_id, []).append(text)
            session.status = DevelopmentStatus.WAITING_CODEX.value
            self.store.put(session)
            return {"ok": True, "queued": True, "plugin_id": session.plugin_id, "thread_id": session.thread_id}
        turn_id = await server.start_turn(text)
        session.current_turn_id = turn_id
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

    def events(self, plugin_id: str, limit: int = 5) -> list[dict[str, Any]]:
        session = self._require(plugin_id)
        return session.events[-max(1, min(int(limit), 20)):]

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
                command=self.config.command,
                cwd=Path(session.workspace_path),
                codex_home=self.config.runtime_codex_home,
                approval_policy=self.config.approval_policy,
                approvals_reviewer=self.config.approvals_reviewer,
                sandbox=_app_server_sandbox(self.config.sandbox),
                timeout_seconds=self.config.app_server_timeout_seconds,
                on_event=lambda message: self._on_message(session.plugin_id, message),
            )
            self.servers[session.plugin_id] = server
            old_thread = session.thread_id
            try:
                session.thread_id = await server.start(thread_id=old_thread)
            except Exception as exc:
                self.servers.pop(session.plugin_id, None)
                session.status = DevelopmentStatus.FAILED.value
                session.last_error = str(exc)
                self.store.put(session)
                raise
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
            event_type = _event_type(method)
        if event_type is None:
            return
        text = _event_text(method, params)
        if method == "turn/started":
            session.current_turn_id = str(params.get("turn", {}).get("id") or session.current_turn_id)
            session.status = DevelopmentStatus.RUNNING.value
        elif event_type == DevelopmentEventType.TURN_COMPLETED:
            session.current_turn_id = ""
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
            session.status = DevelopmentStatus.FAILED.value if event_type == DevelopmentEventType.ERROR else DevelopmentStatus.STALE.value
            session.last_error = text
        await self._record(session, event_type, text, metadata={"method": method, "params": _compact(params)})
        await self._notify(session, event_type, text)

    async def _record(self, session, event_type, text, metadata=None) -> None:
        event = DevelopmentEvent(
            event_id=f"dev:{uuid4().hex}", plugin_id=session.plugin_id, event_type=event_type.value,
            text=text, thread_id=session.thread_id, turn_id=session.current_turn_id, metadata=metadata or {},
        )
        session.last_result = text if event_type in {DevelopmentEventType.ASSISTANT_MESSAGE, DevelopmentEventType.TURN_COMPLETED} else session.last_result
        session.events.append(event.to_dict())
        self.store.put(session)

    async def _notify(self, session, event_type, text) -> None:
        if self._active_ctx is None:
            return
        sessions = set(self.config.notify_sessions or [])
        if not sessions:
            return
        instruction = wrap_event(plugin_id=session.plugin_id, thread_id=session.thread_id, event_type=event_type.value, text=text)
        for session_key in sessions:
            try:
                await self._active_ctx.resources.conversation.submit_intent(ActiveConversationIntent(
                    intent_id=f"codex:{session.plugin_id}:{uuid4().hex}", session_key=session_key,
                    kind=f"codex_{event_type.value}", instruction=instruction,
                    evidence={"plugin_id": session.plugin_id, "thread_id": session.thread_id, "event_type": event_type.value},
                    request_id=f"codex-event:{session.plugin_id}:{uuid4().hex}",
                ))
            except Exception:
                # Event persistence remains authoritative if delivery is temporarily unavailable.
                continue

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

    def _write_scaffold(self, workspace: Path, plugin_id: str, description: str, brief: str) -> None:
        (workspace / ".codex-plugin").mkdir(exist_ok=True)
        (workspace / ".codex-plugin" / "plugin.json").write_text(json.dumps({"name": plugin_id, "version": "0.1.0", "description": description}, indent=2) + "\n", encoding="utf-8")
        (workspace / "plugin.yaml").write_text(f"schema_version: 1\nkey: external/{plugin_id}\nname: {plugin_id}\nversion: \"0.1.0\"\nplugin_api: \">=1,<2\"\nentrypoint: plugin:register\nprovides: [tool]\n", encoding="utf-8")
        (workspace / "PLUGIN_BRIEF.md").write_text(f"# {plugin_id}\n\n{description}\n\n{brief}\n", encoding="utf-8")
        spec = Path(self.config.development_spec_path)
        if spec.is_file():
            shutil.copyfile(spec, workspace / "LUNA_PLUGIN_DEVELOPMENT.md")


def _event_type(method: str) -> DevelopmentEventType | None:
    if method in {"turn/started", "turn.started"}:
        return DevelopmentEventType.TURN_STARTED
    if method in {"item/agentMessage/completed", "item/completed"}:
        return DevelopmentEventType.ASSISTANT_MESSAGE
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
    item = params.get("item")
    if isinstance(item, dict):
        for key in ("text", "content", "summary", "command"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return method or "Codex event"


def _compact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _compact(v) for k, v in list(value.items())[:20]}
    if isinstance(value, list):
        return [_compact(v) for v in value[:20]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value if not isinstance(value, str) else value[:1000]
    return str(value)


def _app_server_sandbox(value: str) -> str:
    return {
        "read-only": "readOnly",
        "workspace-write": "workspaceWrite",
    }.get(str(value), str(value))
