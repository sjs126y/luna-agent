from __future__ import annotations

import io

import pytest

from luna_agent_plugin_sdk.worker_protocol import (
    WorkerProtocolError,
    read_frame,
    write_frame,
)


def test_frame_round_trip() -> None:
    stream = io.BytesIO()
    write_frame(stream, {"type": "request", "payload": {"text": "hello"}})
    stream.seek(0)

    assert read_frame(stream) == {"type": "request", "payload": {"text": "hello"}}


def test_frame_rejects_oversized_payload() -> None:
    with pytest.raises(WorkerProtocolError, match="exceeds limit"):
        write_frame(io.BytesIO(), {"value": "x" * 100}, max_message_bytes=32)
