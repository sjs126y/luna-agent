"""Proactively submit stable workspace file changes to the main conversation."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from personal_agent.plugins import ActiveResourceRequest, CommandEntry


class ActiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    sessions: list[str] = Field(default_factory=list)
    restart_backoff_seconds: list[float] = Field(
        default_factory=lambda: [1, 2, 5, 10, 30]
    )


class WorkspaceWatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(default_factory=list)
    session_key: str = ""
    poll_interval_seconds: float = Field(default=30.0, ge=1.0)
    settle_seconds: float = Field(default=10.0, ge=0.0)
    prompt: str = (
        "工作区里这些文件刚刚发生了稳定变化。请查看变化涉及的文件，判断是否有需要提醒我的事项；"
        "如果没有值得打扰我的内容，直接简短说明。"
    )
    active: ActiveConfig = Field(default_factory=ActiveConfig)


@dataclass
class PendingChange:
    signature: str
    first_seen: float


class WorkspaceWatcher:
    def __init__(self, ctx, config: WorkspaceWatchConfig) -> None:
        self.ctx = ctx
        self.config = config
        self.storage = ctx.resources.storage
        self.signatures = self._load_signatures()
        self.pending: dict[str, PendingChange] = {}

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
        changed: list[str] = []
        signatures_changed = False
        for path in self.config.paths:
            signature = await self._signature(path)
            if not signature:
                continue
            previous = self.signatures.get(path)
            if previous is None:
                self.signatures[path] = signature
                signatures_changed = True
                self.pending.pop(path, None)
                continue
            if signature == previous:
                self.pending.pop(path, None)
                continue
            candidate = self.pending.get(path)
            if candidate is None or candidate.signature != signature:
                self.pending[path] = PendingChange(signature, current_time)
                if self.config.settle_seconds > 0:
                    continue
                candidate = self.pending[path]
            if current_time - candidate.first_seen < self.config.settle_seconds:
                continue
            self.signatures[path] = signature
            signatures_changed = True
            self.pending.pop(path, None)
            changed.append(path)

        if signatures_changed:
            self._save_signatures()
        if changed and self.config.session_key:
            await self.ctx.resources.conversation.submit(
                session_key=self.config.session_key,
                text=self._prompt(changed),
                metadata={"plugin": "workspace-watch", "changed_paths": changed},
            )
            self._write_status("notified", changed_paths=changed)
        return changed

    async def _signature(self, path: str) -> str:
        result = await self.ctx.resources.tool.call("file_info", {"path": path})
        if str(getattr(result, "status", "")) != "success":
            return ""
        content = getattr(result, "content", "")
        if isinstance(content, dict):
            payload = content
        else:
            try:
                payload = json.loads(str(content or ""))
            except json.JSONDecodeError:
                return ""
        if not isinstance(payload, dict):
            return ""
        stable = {
            "path": str(payload.get("path") or path),
            "type": str(payload.get("type") or ""),
            "size_bytes": payload.get("size_bytes"),
            "modified_at": str(payload.get("modified_at") or ""),
        }
        return json.dumps(stable, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    def _prompt(self, changed: list[str]) -> str:
        paths = "\n".join(f"- {path}" for path in changed)
        return f"{self.config.prompt.strip()}\n\n发生变化的文件：\n{paths}"

    def _load_signatures(self) -> dict[str, str]:
        raw = self.storage.read_text("signatures.json", default="")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            str(path): str(signature)
            for path, signature in payload.items()
            if str(path) and str(signature)
        }

    def _save_signatures(self) -> None:
        self.storage.write_text(
            "signatures.json",
            json.dumps(self.signatures, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        )

    def _write_status(self, state: str, **details: Any) -> None:
        self.storage.write_text(
            "status.json",
            json.dumps(
                {"state": state, **details},
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            ) + "\n",
        )


def register(ctx) -> None:
    config = ctx.parse_config(WorkspaceWatchConfig)

    async def run(active_ctx) -> None:
        await WorkspaceWatcher(active_ctx, config).run()

    ctx.register.active(
        run=run,
        resources=ActiveResourceRequest(
            tools=("file_info",),
            conversation=True,
        ),
        restart_policy="on_failure",
        startup_timeout=15,
        shutdown_timeout=15,
    )
    ctx.register.command(CommandEntry(
        name="workspace-watch-status",
        description="Show Workspace Watch configuration and current active state.",
        handler=lambda args="", **kwargs: _status(ctx, config),
        scope="both",
    ))


def _status(ctx, config: WorkspaceWatchConfig) -> str:
    runtime = ctx.runtime.safe_summary()
    paths = ", ".join(config.paths) if config.paths else "none"
    return (
        "Workspace Watch\n"
        f"- active: {'enabled' if config.active.enabled else 'disabled'}\n"
        f"- runtime: {runtime['state']}\n"
        f"- session: {config.session_key or 'not configured'}\n"
        f"- paths: {paths}\n"
        f"- interval: {config.poll_interval_seconds:g}s\n"
        f"- settle: {config.settle_seconds:g}s"
    )
