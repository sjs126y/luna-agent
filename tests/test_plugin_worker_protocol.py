from __future__ import annotations

from datetime import UTC, date, datetime, time
import io

import pytest

from luna_agent_plugin_sdk.worker_protocol import (
    FramedRPCPeer,
    WorkerProtocolError,
    read_frame,
    write_frame,
    from_wire,
    to_wire,
)
from luna_agent_plugin_sdk.worker import RemoteResourceNamespace


def test_frame_round_trip() -> None:
    stream = io.BytesIO()
    write_frame(stream, {"type": "request", "payload": {"text": "hello"}})
    stream.seek(0)

    assert read_frame(stream) == {"type": "request", "payload": {"text": "hello"}}


def test_frame_rejects_oversized_payload() -> None:
    with pytest.raises(WorkerProtocolError, match="exceeds limit"):
        write_frame(io.BytesIO(), {"value": "x" * 100}, max_message_bytes=32)


def test_temporal_values_round_trip_through_wire_protocol() -> None:
    value = {
        "datetime": datetime(2026, 7, 21, 22, 47, 53, 123456, tzinfo=UTC),
        "date": date(2026, 7, 21),
        "time": time(22, 47, 53, 123456, tzinfo=UTC),
    }

    assert from_wire(to_wire(value)) == value


def test_unsupported_values_remain_rejected_by_wire_encoder() -> None:
    # Keep the low-level encoder strict for accidental objects; the RPC
    # dispatcher is responsible for turning this into a structured response.
    with pytest.raises(TypeError, match="not plugin-RPC serializable"):
        to_wire(object())


@pytest.mark.asyncio
async def test_dispatch_converts_serialization_failure_to_rpc_error() -> None:
    peer = FramedRPCPeer(io.BytesIO(), io.BytesIO())
    peer.register("returns-unsupported", lambda _payload: object())
    sent: list[dict] = []

    async def capture(message: dict) -> None:
        sent.append(message)

    peer._send = capture
    await peer._dispatch({
        "protocol_version": 1,
        "type": "request",
        "id": "request-1",
        "method": "returns-unsupported",
        "payload": {},
    })

    assert sent == [{
        "protocol_version": 1,
        "type": "response",
        "id": "request-1",
        "ok": False,
        "error": {
            "code": "TypeError",
            "message": "Value is not plugin-RPC serializable: object",
        },
    }]


@pytest.mark.asyncio
async def test_process_resource_uses_extended_rpc_timeout() -> None:
    class Peer:
        async def call(self, _method, _payload, *, timeout):
            self.timeout = timeout
            return {"ok": True}

    peer = Peer()
    namespace = RemoteResourceNamespace(peer, "process", rpc_timeout=120)

    assert await namespace.read_line(process_id="codex") == {"ok": True}
    assert peer.timeout == 120
