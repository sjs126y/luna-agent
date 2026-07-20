"""Small JSON-RPC client for the Codex App Server stdio protocol."""

from __future__ import annotations

import asyncio
import json
import os
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
        command: str,
        cwd: Path,
        codex_home: Path,
        approval_policy: str = "on-request",
        approvals_reviewer: str = "user",
        sandbox: str = "workspaceWrite",
        timeout_seconds: float = 30.0,
        on_event: EventCallback | None = None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.codex_home = codex_home
        self.approval_policy = approval_policy
        self.approvals_reviewer = approvals_reviewer
        self.sandbox = sandbox
        self.timeout_seconds = timeout_seconds
        self.on_event = on_event
        self.process: asyncio.subprocess.Process | None = None
        self.thread_id = ""
        self.active_turn_id = ""
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
        return self.process is not None and self.process.returncode is None and not self._closed

    async def start(self, *, thread_id: str = "") -> str:
        if self.running:
            return self.thread_id
        self._closed = False
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.codex_home)
        self.process = await asyncio.create_subprocess_exec(
            self.command,
            "app-server",
            cwd=str(self.cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
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
        if thread_id:
            try:
                result = await self._request("thread/resume", {
                    "threadId": thread_id,
                    "approvalsReviewer": self.approvals_reviewer,
                })
            except CodexAppServerError:
                result = await self._request("thread/start", self._thread_params())
        else:
            result = await self._request("thread/start", self._thread_params())
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
        if self._closed and self.process is None:
            return
        self._closed = True
        process = self.process
        self.process = None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
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
        return {
            "cwd": str(self.cwd),
            "approvalPolicy": self.approval_policy,
            "approvalsReviewer": self.approvals_reviewer,
            "sandbox": self.sandbox,
        }

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
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise CodexAppServerError("Codex App Server is not running")
        payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            process.stdin.write(payload)
            await process.stdin.drain()

    async def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            async for line in process.stdout:
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
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
                await self._emit({
                    "method": "codex/processExited",
                    "params": {"returncode": process.returncode, "stderr": self.last_stderr},
                })
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit({"method": "codex/processError", "params": {"error": str(exc)}})

    async def _read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            async for line in process.stderr:
                self.last_stderr = line.decode("utf-8", errors="replace").strip()[-2000:]
        except asyncio.CancelledError:
            raise

    async def _emit(self, message: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        result = self.on_event(message)
        if asyncio.iscoroutine(result):
            await result
