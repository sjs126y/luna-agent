"""Controlled inbox directory watcher with Artifact submission."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from personal_agent.plugins import ActiveResourceRequest, CommandEntry


class ActiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    sessions: list[str] = Field(default_factory=list)
    restart_backoff_seconds: list[float] = Field(default_factory=lambda: [1, 2, 5, 10, 30])


class InboxWatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: str = "data/inbox"
    poll_interval_seconds: float = Field(default=15.0, ge=1.0)
    settle_seconds: float = Field(default=5.0, ge=0.0)
    max_files_per_poll: int = Field(default=20, ge=1, le=100)
    max_file_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    max_submission_attempts: int = Field(default=3, ge=1, le=20)
    extensions: list[str] = Field(default_factory=list)
    process_existing: bool = True
    prompt: str = (
        "Inbox Watch 收到一个新文件。请检查附件并告诉用户文件的主要内容、值得注意的事项，"
        "以及适合的下一步；不要声称修改或删除了原文件。"
    )
    active: ActiveConfig = Field(default_factory=ActiveConfig)


@dataclass
class PendingFile:
    signature: str
    first_seen: float


class InboxWatcher:
    def __init__(self, ctx, config: InboxWatchConfig) -> None:
        self.ctx = ctx
        self.config = config
        self.storage = ctx.resources.storage
        self.state = self.storage.read_json(
            "inbox-state.json",
            default={"schema_version": 1, "processed": {}, "failures": {}},
            schema_version=1,
        )
        self.pending: dict[str, PendingFile] = {}

    async def run(self) -> None:
        self._write_status("starting")
        await self.ctx.runtime.ready()
        self._write_status("active")
        while not self.ctx.runtime.stop_requested:
            await self.ctx.runtime.wait_until_resumed()
            self.ctx.runtime.heartbeat()
            await self.poll_once()
            try:
                await asyncio.wait_for(
                    self.ctx.runtime.wait_until_stopped(),
                    timeout=self.config.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass
        self._write_status("stopped")

    async def poll_once(self, *, now: float | None = None) -> list[str]:
        current_time = time.monotonic() if now is None else float(now)
        files = await self._list_files()
        processed_now: list[str] = []
        for path in files[: self.config.max_files_per_poll]:
            info = await self._file_info(path)
            if not info or info.get("type") != "file":
                continue
            size = int(info.get("size_bytes") or 0)
            if size <= 0 or size > self.config.max_file_bytes:
                self._record_failure(path, f"file size {size} is outside configured limits")
                continue
            signature = _signature(info)
            failure = dict(self.state["failures"].get(path) or {})
            if (
                failure.get("signature") == signature
                and int(failure.get("attempts") or 0) >= self.config.max_submission_attempts
            ):
                self.pending.pop(path, None)
                continue
            previous = dict(self.state["processed"].get(path) or {})
            if previous.get("signature") == signature:
                self.pending.pop(path, None)
                continue
            if not previous and not self.config.process_existing:
                self.state["processed"][path] = {
                    "signature": signature,
                    "processed_at": "",
                    "baseline_only": True,
                }
                self._save_state()
                continue
            candidate = self.pending.get(path)
            if candidate is None or candidate.signature != signature:
                self.pending[path] = PendingFile(signature, current_time)
                if self.config.settle_seconds > 0:
                    continue
                candidate = self.pending[path]
            if current_time - candidate.first_seen < self.config.settle_seconds:
                continue
            if await self._submit_file(path, signature):
                self.state["processed"][path] = {
                    "signature": signature,
                    "processed_at": datetime.now(UTC).isoformat(),
                    "baseline_only": False,
                }
                self.state["failures"].pop(path, None)
                self.pending.pop(path, None)
                self._save_state()
                processed_now.append(path)
        self._write_status(
            "active",
            processed=len(self.state["processed"]),
            pending=len(self.pending),
            failures=len(self.state["failures"]),
        )
        return processed_now

    async def _list_files(self) -> list[str]:
        result = await self.ctx.resources.tool.call("list_directory", {
            "path": self.config.root,
            "offset": 0,
            "limit": min(500, self.config.max_files_per_poll * 5),
            "include_hidden": False,
        })
        payload = _tool_json(result)
        root = Path(str(payload.get("path") or self.config.root))
        allowed = {value.lower() if value.startswith(".") else f".{value.lower()}" for value in self.config.extensions}
        files = []
        for item in payload.get("entries", []):
            if not isinstance(item, dict) or item.get("type") != "file":
                continue
            name = str(item.get("name") or "")
            if not name or (allowed and Path(name).suffix.lower() not in allowed):
                continue
            files.append(str(root / name))
        return files

    async def _file_info(self, path: str) -> dict[str, Any]:
        result = await self.ctx.resources.tool.call("file_info", {"path": path})
        return _tool_json(result)

    async def _submit_file(self, path: str, signature: str) -> bool:
        for session_key in self.config.active.sessions:
            result = await self.ctx.resources.tool.call(
                "artifact_from_file",
                {"path": path, "filename": Path(path).name},
                session_key=session_key,
            )
            if str(getattr(result, "status", "")) != "success" or not getattr(result, "artifacts", None):
                self._record_failure(
                    path,
                    str(getattr(result, "error", "") or getattr(result, "content", "")),
                    signature=signature,
                )
                return False
            artifact_id = str(getattr(result.artifacts[0], "artifact_id", ""))
            if not artifact_id:
                self._record_failure(path, "artifact tool returned no artifact_id", signature=signature)
                return False
            request_hash = hashlib.sha256(f"{path}:{signature}:{session_key}".encode()).hexdigest()[:24]
            try:
                handle = await self.ctx.resources.conversation.submit(
                    session_key=session_key,
                    text=f"{self.config.prompt}\n\n文件名：{Path(path).name}",
                    request_id=f"inbox-watch:{request_hash}",
                    artifact_ids=[artifact_id],
                    metadata={"plugin": "inbox-watch", "inbox_path": path, "signature": signature},
                )
                outcome_method = getattr(handle, "outcome", None)
                if callable(outcome_method):
                    outcome = await outcome_method()
                    if not bool(getattr(outcome, "succeeded", False)):
                        raise RuntimeError(str(getattr(outcome, "error", "submission failed")))
            except Exception as exc:
                self._record_failure(path, f"{type(exc).__name__}: {exc}", signature=signature)
                return False
        return bool(self.config.active.sessions)

    def _record_failure(self, path: str, error: str, *, signature: str = "") -> None:
        current = dict(self.state["failures"].get(path) or {})
        attempts = (
            int(current.get("attempts") or 0) + 1
            if not signature or current.get("signature") in {None, "", signature}
            else 1
        )
        self.state["failures"][path] = {
            "attempts": attempts,
            "signature": signature,
            "error": str(error)[:1000],
            "updated_at": datetime.now(UTC).isoformat(),
        }
        self._save_state()

    def _save_state(self) -> None:
        self.storage.write_json_atomic("inbox-state.json", self.state)

    def _write_status(self, state: str, **details: Any) -> None:
        self.storage.write_json_atomic(
            "inbox-status.json",
            {"schema_version": 1, "state": state, "updated_at": datetime.now(UTC).isoformat(), **details},
        )


def register(ctx) -> None:
    config = ctx.parse_config(InboxWatchConfig)

    async def run(active_ctx) -> None:
        await InboxWatcher(active_ctx, config).run()

    ctx.register.active(
        run=run,
        resources=ActiveResourceRequest(
            tools=("list_directory", "file_info", "artifact_from_file"),
            conversation=True,
        ),
        restart_policy="on_failure",
        startup_timeout=15,
        shutdown_timeout=15,
    )
    ctx.register.command(CommandEntry(
        "inbox-status",
        "Show Inbox Watch runtime and processing status.",
        lambda args="", **kwargs: _status(ctx, config),
        scope="both",
    ))


def _tool_json(result) -> dict[str, Any]:
    if str(getattr(result, "status", "")) != "success":
        raise RuntimeError(str(getattr(result, "error", "") or getattr(result, "content", "")))
    content = getattr(result, "content", "")
    if isinstance(content, dict):
        return content
    try:
        payload = json.loads(str(content or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("inbox tool returned non-JSON content") from exc
    if not isinstance(payload, dict):
        raise ValueError("inbox tool result must be an object")
    return payload


def _signature(info: dict[str, Any]) -> str:
    payload = {
        "path": str(info.get("path") or ""),
        "size_bytes": int(info.get("size_bytes") or 0),
        "modified_at": str(info.get("modified_at") or ""),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _status(ctx, config: InboxWatchConfig) -> str:
    status = ctx.storage.read_json("inbox-status.json", default={}) or {}
    return (
        "Inbox Watch\n"
        f"- active: {'enabled' if config.active.enabled else 'disabled'}\n"
        f"- runtime: {status.get('state') or ctx.runtime.safe_summary().get('state')}\n"
        f"- root: {config.root}\n"
        f"- processed: {status.get('processed', 0)}\n"
        f"- pending: {status.get('pending', 0)}\n"
        f"- failures: {status.get('failures', 0)}"
    )
