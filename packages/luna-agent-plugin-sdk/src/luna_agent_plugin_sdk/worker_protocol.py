"""Small framed JSON-RPC transport shared by the host and plugin workers."""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4


PROTOCOL_VERSION = 1
DEFAULT_MAX_MESSAGE_BYTES = 8 * 1024 * 1024
_HEADER = struct.Struct(">I")


class WorkerProtocolError(RuntimeError):
    pass


Handler = Callable[[dict[str, Any]], Awaitable[Any] | Any]


class FramedRPCPeer:
    def __init__(
        self,
        reader: BinaryIO,
        writer: BinaryIO,
        *,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.max_message_bytes = max(1024, int(max_message_bytes))
        self.handlers: dict[str, Handler] = {}
        self.pending: dict[str, asyncio.Future[Any]] = {}
        self.dispatch_tasks: set[asyncio.Task[None]] = set()
        self.write_lock = asyncio.Lock()
        self.reader_task: asyncio.Task[None] | None = None
        self.closed = False
        self.last_error = ""

    def register(self, method: str, handler: Handler) -> None:
        self.handlers[str(method)] = handler

    async def start(self) -> None:
        if self.reader_task is None:
            self.reader_task = asyncio.create_task(self._read_loop(), name="plugin-rpc-reader")

    async def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> Any:
        if self.closed:
            raise WorkerProtocolError("plugin RPC channel is closed")
        request_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self._send({
            "protocol_version": PROTOCOL_VERSION,
            "type": "request",
            "id": request_id,
            "method": str(method),
            "payload": to_wire(payload or {}),
        })
        try:
            return await asyncio.wait_for(future, timeout=max(0.1, float(timeout)))
        except asyncio.TimeoutError as exc:
            await self.notify("cancel", {"request_id": request_id})
            raise WorkerProtocolError(f"plugin RPC request timed out: {method}") from exc
        finally:
            self.pending.pop(request_id, None)

    async def notify(self, method: str, payload: dict[str, Any] | None = None) -> None:
        if self.closed:
            return
        await self._send({
            "protocol_version": PROTOCOL_VERSION,
            "type": "notification",
            "method": str(method),
            "payload": to_wire(payload or {}),
        })

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for future in tuple(self.pending.values()):
            if not future.done():
                future.set_exception(WorkerProtocolError("plugin RPC channel closed"))
        self.pending.clear()
        if self.reader_task is not None and self.reader_task is not asyncio.current_task():
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)
        for task in tuple(self.dispatch_tasks):
            task.cancel()
        if self.dispatch_tasks:
            await asyncio.gather(*self.dispatch_tasks, return_exceptions=True)

    async def _read_loop(self) -> None:
        try:
            while not self.closed:
                message = await asyncio.to_thread(
                    read_frame,
                    self.reader,
                    self.max_message_bytes,
                )
                if message is None:
                    raise EOFError("plugin RPC stream closed")
                if str(message.get("type") or "") == "response":
                    await self._dispatch(message)
                else:
                    task = asyncio.create_task(
                        self._dispatch(message),
                        name=f"plugin-rpc:{message.get('method') or 'message'}",
                    )
                    self.dispatch_tasks.add(task)
                    task.add_done_callback(self.dispatch_tasks.discard)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            if not self.closed:
                self.closed = True
                for future in tuple(self.pending.values()):
                    if not future.done():
                        future.set_exception(
                            WorkerProtocolError(self.last_error or "plugin RPC stream closed")
                        )

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if int(message.get("protocol_version") or 0) != PROTOCOL_VERSION:
            raise WorkerProtocolError("plugin RPC protocol version mismatch")
        message_type = str(message.get("type") or "")
        if message_type == "response":
            request_id = str(message.get("id") or "")
            future = self.pending.get(request_id)
            if future is None or future.done():
                return
            if bool(message.get("ok")):
                future.set_result(from_wire(message.get("payload")))
            else:
                error = message.get("error") or {}
                future.set_exception(WorkerProtocolError(
                    str(error.get("message") or "plugin RPC request failed")
                ))
            return
        if message_type not in {"request", "notification"}:
            raise WorkerProtocolError(f"unsupported plugin RPC message type: {message_type}")
        method = str(message.get("method") or "")
        handler = self.handlers.get(method)
        if handler is None:
            if message_type == "request":
                await self._respond_error(message, "method_not_found", f"Unknown method: {method}")
            return
        try:
            result = handler(from_wire(message.get("payload") or {}))
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:
            if message_type == "request":
                await self._respond_error(message, type(exc).__name__, str(exc))
            return
        if message_type == "request":
            await self._send({
                "protocol_version": PROTOCOL_VERSION,
                "type": "response",
                "id": str(message.get("id") or ""),
                "ok": True,
                "payload": to_wire(result),
            })

    async def _respond_error(self, message: dict[str, Any], code: str, text: str) -> None:
        await self._send({
            "protocol_version": PROTOCOL_VERSION,
            "type": "response",
            "id": str(message.get("id") or ""),
            "ok": False,
            "error": {"code": str(code), "message": str(text)},
        })

    async def _send(self, message: dict[str, Any]) -> None:
        async with self.write_lock:
            await asyncio.to_thread(
                write_frame,
                self.writer,
                message,
                self.max_message_bytes,
            )


def read_frame(reader: BinaryIO, max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> dict[str, Any] | None:
    header = _read_exact(reader, _HEADER.size)
    if not header:
        return None
    size = _HEADER.unpack(header)[0]
    if size <= 0 or size > max_message_bytes:
        raise WorkerProtocolError(f"plugin RPC frame exceeds limit: {size}")
    payload = _read_exact(reader, size)
    if len(payload) != size:
        raise EOFError("plugin RPC frame ended early")
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise WorkerProtocolError("plugin RPC frame must contain an object")
    return value


def write_frame(
    writer: BinaryIO,
    message: dict[str, Any],
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
) -> None:
    payload = json.dumps(
        message,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not payload or len(payload) > max_message_bytes:
        raise WorkerProtocolError(f"plugin RPC payload exceeds limit: {len(payload)}")
    writer.write(_HEADER.pack(len(payload)))
    writer.write(payload)
    writer.flush()


def to_wire(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        import base64

        return {"__type__": "bytes", "data": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Path):
        return {"__type__": "path", "value": str(value)}
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "__type__": type(value).__name__,
            "fields": {field.name: to_wire(getattr(value, field.name)) for field in fields(value)},
        }
    if isinstance(value, Mapping):
        return {str(key): to_wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_wire(item) for item in value]
    if hasattr(value, "model_dump"):
        return to_wire(value.model_dump(mode="json"))
    raise TypeError(f"Value is not plugin-RPC serializable: {type(value).__name__}")


def from_wire(value: Any) -> Any:
    if isinstance(value, list):
        return [from_wire(item) for item in value]
    if not isinstance(value, dict):
        return value
    type_name = str(value.get("__type__") or "")
    if type_name == "bytes":
        import base64

        return base64.b64decode(str(value.get("data") or ""), validate=True)
    if type_name == "path":
        return Path(str(value.get("value") or ""))
    if type_name:
        mapped = _wire_types().get(type_name)
        fields_value = value.get("fields") or {}
        if mapped is not None and isinstance(fields_value, dict):
            return mapped(**{key: from_wire(item) for key, item in fields_value.items()})
    return {str(key): from_wire(item) for key, item in value.items()}


def _wire_types() -> dict[str, type]:
    from luna_agent_plugin_sdk.active import ActiveConversationIntent, ConversationStatus
    from luna_agent_plugin_sdk.hooks import (
        ContextHookOutcome,
        GatewayMessageOutcome,
        HookEnvelope,
        PermissionRequestOutcome,
        PostToolUseOutcome,
        PreDeliveryOutcome,
        PreToolUseOutcome,
        StopOutcome,
    )
    from luna_agent_plugin_sdk.tools import ToolArtifact, ToolHandlerOutput

    types = (
        ActiveConversationIntent,
        ConversationStatus,
        ContextHookOutcome,
        GatewayMessageOutcome,
        HookEnvelope,
        PermissionRequestOutcome,
        PostToolUseOutcome,
        PreDeliveryOutcome,
        PreToolUseOutcome,
        StopOutcome,
        ToolArtifact,
        ToolHandlerOutput,
    )
    return {item.__name__: item for item in types}


def _read_exact(reader: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = reader.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
