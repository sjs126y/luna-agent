"""Small JSON-RPC client for the Codex App Server stdio protocol."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any


class CodexAppServerError(RuntimeError):
    pass


EventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class CodexAppServer:
    """Own one app-server process and one logical Codex thread."""

    def __init__(
        self,
        *,
        process_port: Any,
        process_name: str,
        cwd: Path,
        codex_home: Path,
        approval_policy: str = "on-request",
        approvals_reviewer: str = "user",
        sandbox: str = "workspace-write",
        timeout_seconds: float = 30.0,
        on_event: EventCallback | None = None,
    ) -> None:
        self.process_port = process_port
        self.process_name = process_name
        self.cwd = cwd
        self.codex_home = codex_home
        self.approval_policy = approval_policy
        self.approvals_reviewer = approvals_reviewer
        self.sandbox = sandbox
        self.timeout_seconds = timeout_seconds
        self.on_event = on_event
        self.process_id = ""
        self.thread_id = ""
        self.active_turn_id = ""
        self.effective_model = ""
        self.effective_model_provider = ""
        self.effective_service_tier: str | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._server_requests: dict[str, dict[str, Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._closed = False
        self.last_stderr = ""

    @property
    def running(self) -> bool:
        return bool(self.process_id and not self._closed)

    async def start(self, *, thread_id: str = "") -> str:
        if self.running:
            return self.thread_id
        self._closed = False
        started = await self.process_port.start(
            name=self.process_name,
            cwd=str(self.cwd),
            env={"CODEX_HOME": str(self.codex_home)},
        )
        self.process_id = str(_value(started, "process_id") or "")
        if not self.process_id:
            raise CodexAppServerError("Host process port did not return a process id")
        self._reader_task = asyncio.create_task(self._read_stdout(), name="codex-app-server-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="codex-app-server-stderr")
        await self._request(
            "initialize",
            {
                "clientInfo": {"name": "luna-agent", "version": "0.1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self._notify("initialized", {})
        await self._load_effective_config()
        if thread_id:
            try:
                result = await self._request("thread/resume", self._resume_params(thread_id))
            except CodexAppServerError:
                result = await self._request("thread/start", self._thread_params())
        else:
            result = await self._request("thread/start", self._thread_params())
        self._validate_thread_config(result)
        self.thread_id = str(result.get("thread", {}).get("id") or thread_id)
        if not self.thread_id:
            await self.close()
            raise CodexAppServerError("Codex did not return a thread id")
        return self.thread_id

    async def start_turn(self, text: str) -> str:
        if not self.running:
            await self.start(thread_id=self.thread_id)
        if not self.thread_id:
            raise CodexAppServerError("Codex thread is not initialized")
        if self.active_turn_id:
            raise CodexAppServerError("Codex thread already has an active turn")
        result = await self._request("turn/start", {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": str(text)}],
            "approvalPolicy": self.approval_policy,
            "approvalsReviewer": self.approvals_reviewer,
            "sandbox": self.sandbox,
            "cwd": str(self.cwd),
        })
        self.active_turn_id = str(result.get("turn", {}).get("id") or "")
        return self.active_turn_id

    async def interrupt(self) -> None:
        if self.running and self.thread_id and self.active_turn_id:
            await self._request("turn/interrupt", {
                "threadId": self.thread_id,
                "turnId": self.active_turn_id,
            })

    async def respond(self, request_id: str, result: dict[str, Any]) -> None:
        if not self.running:
            raise CodexAppServerError("Codex App Server is not running")
        if str(request_id) not in self._server_requests:
            raise CodexAppServerError(f"Unknown Codex server request: {request_id}")
        await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})
        self._server_requests.pop(str(request_id), None)

    async def close(self, *, timeout: float = 10.0) -> None:
        if self._closed and not self.process_id:
            return
        self._closed = True
        process_id = self.process_id
        self.process_id = ""
        if not process_id:
            return
        try:
            await asyncio.wait_for(
                self.process_port.stop(process_id=process_id),
                timeout=timeout,
            )
        except Exception:
            pass
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(self._reader_task, self._stderr_task, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(CodexAppServerError("Codex App Server closed"))
        self._pending.clear()
        self.active_turn_id = ""

    def _thread_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.effective_model,
            "modelProvider": self.effective_model_provider,
            "cwd": str(self.cwd),
            "approvalPolicy": self.approval_policy,
            "approvalsReviewer": self.approvals_reviewer,
            "sandbox": self.sandbox,
        }
        if self.effective_service_tier is not None:
            params["serviceTier"] = self.effective_service_tier
        return params

    def _resume_params(self, thread_id: str) -> dict[str, Any]:
        return {"threadId": str(thread_id), **self._thread_params()}

    async def _load_effective_config(self) -> None:
        result = await self._request("config/read", {
            "includeLayers": False,
            "cwd": str(self.cwd),
        })
        config = result.get("config") or {}
        self.effective_model = str(config.get("model") or "").strip()
        self.effective_model_provider = str(
            config.get("modelProvider") or config.get("model_provider") or ""
        ).strip()
        service_tier = (
            config.get("serviceTier")
            if "serviceTier" in config
            else config.get("service_tier")
        )
        self.effective_service_tier = (
            str(service_tier).strip() if service_tier is not None else None
        )
        if not self.effective_model:
            raise CodexAppServerError("Codex effective config did not resolve a model")
        if not self.effective_model_provider:
            raise CodexAppServerError("Codex effective config did not resolve a model provider")

    def _validate_thread_config(self, result: dict[str, Any]) -> None:
        thread = result.get("thread") or {}
        actual_model = str(result.get("model") or thread.get("model") or "").strip()
        actual_provider = str(
            result.get("modelProvider") or thread.get("modelProvider") or ""
        ).strip()
        if actual_model != self.effective_model:
            raise CodexAppServerError(
                "Codex thread model mismatch: "
                f"expected {self.effective_model!r}, got {actual_model or '<missing>'!r}"
            )
        if actual_provider != self.effective_model_provider:
            raise CodexAppServerError(
                "Codex thread provider mismatch: "
                f"expected {self.effective_model_provider!r}, "
                f"got {actual_provider or '<missing>'!r}"
            )

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            response = await asyncio.wait_for(future, timeout=self.timeout_seconds)
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            error = response.get("error") or {}
            raise CodexAppServerError(str(error.get("message") or error))
        return dict(response.get("result") or {})

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write(self, message: dict[str, Any]) -> None:
        if not self.process_id or self._closed:
            raise CodexAppServerError("Codex App Server is not running")
        payload = json.dumps(message, ensure_ascii=False)
        async with self._write_lock:
            await self.process_port.write_line(
                process_id=self.process_id,
                text=payload,
            )

    async def _read_stdout(self) -> None:
        process_id = self.process_id
        if not process_id:
            return
        try:
            while not self._closed and self.process_id == process_id:
                result = await self.process_port.read_line(process_id=process_id)
                line = str(_value(result, "line") or "")
                if bool(_value(result, "eof")):
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "id" in message and message.get("id") in self._pending:
                    future = self._pending[message["id"]]
                    if not future.done():
                        future.set_result(message)
                    continue
                method = str(message.get("method") or "")
                if method and "id" in message:
                    request_id = str(message["id"])
                    self._server_requests[request_id] = message
                if method:
                    await self._emit(message)
            if not self._closed:
                error = CodexAppServerError("Codex App Server exited before replying")
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(error)
                await self._emit({
                    "method": "codex/processExited",
                    "params": {"returncode": _value(result, "returncode"), "stderr": self.last_stderr},
                })
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit({"method": "codex/processError", "params": {"error": str(exc)}})

    async def _read_stderr(self) -> None:
        process_id = self.process_id
        if not process_id:
            return
        try:
            while not self._closed and self.process_id == process_id:
                result = await self.process_port.read_stderr_line(process_id=process_id)
                line = str(_value(result, "line") or "")
                if bool(_value(result, "eof")):
                    break
                self.last_stderr = line.strip()[-2000:]
        except asyncio.CancelledError:
            raise

    async def _emit(self, message: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        result = self.on_event(message)
        if asyncio.iscoroutine(result):
            await result


def _value(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)
