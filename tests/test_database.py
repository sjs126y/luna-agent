"""Test database save→load round-trip for all message types."""

import asyncio
import uuid
from pathlib import Path
import tempfile

import pytest

from personal_agent.db.database import Database


@pytest.fixture
def db():
    """Setup and tear down a test database."""
    path = Path(tempfile.mkdtemp()) / "test.db"
    db_obj = Database(path)

    async def _setup():
        await db_obj.initialize()
        return db_obj

    async def _teardown():
        await db_obj.close()

    asyncio.run(_setup())
    yield db_obj
    asyncio.run(_teardown())


# ── Tests: sync wrappers calling async DB ops ──────────

def _run(coro):
    return asyncio.run(coro)


def test_text_roundtrip(db):
    sid = str(uuid.uuid4())
    _run(db.create_session_direct(sid, "test:1:1"))
    _run(db.save_message(sid, "user", content="hello"))
    _run(db.save_message(sid, "assistant", content="hi there"))

    history = _run(db.load_history(sid))
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"][0]["type"] == "text"
    assert history[0]["content"][0]["text"] == "hello"


def test_tool_use_roundtrip(db):
    """Assistant message with tool_use: text kept, tool_use stripped."""
    sid = str(uuid.uuid4())
    _run(db.create_session_direct(sid, "test:1:1"))
    _run(db.save_message(sid, "assistant", content="ok",
                         tool_calls=[{"id": "c1", "name": "calc", "input": {"expr": "1+1"}}],
                         tool_name="calc"))

    history = _run(db.load_history(sid))
    assert len(history) == 1  # text kept, tool_use stripped
    assert history[0]["content"][0]["text"] == "ok"


def test_tool_result_roundtrip(db):
    """Tool result converted to plain user text."""
    sid = str(uuid.uuid4())
    _run(db.create_session_direct(sid, "test:1:1"))
    _run(db.save_message(sid, "user", content="2", tool_call_id="c1"))

    history = _run(db.load_history(sid))
    assert len(history) == 1
    assert history[0]["content"][0]["text"] == "2"


def test_full_conversation_roundtrip(db):
    """Full conversation: tool messages converted to text, no orphan blocks."""
    sid = str(uuid.uuid4())
    _run(db.create_session_direct(sid, "test:1:1"))
    _run(db.save_message(sid, "user", content="what is 1+1?"))
    _run(db.save_message(sid, "assistant", content="let me check",
                         tool_calls=[{"id": "c1", "name": "calc", "input": {"expr": "1+1"}}],
                         tool_name="calc"))
    _run(db.save_message(sid, "user", content="2", tool_call_id="c1"))
    _run(db.save_message(sid, "assistant", content="1+1 = 2"))

    history = _run(db.load_history(sid))
    assert len(history) == 4  # all messages loaded as text
    assert history[0]["content"][0]["text"] == "what is 1+1?"
    assert history[1]["content"][0]["text"] == "let me check"  # tool_use stripped
    assert history[2]["content"][0]["text"] == "2"             # tool_result → text
    assert history[3]["content"][0]["text"] == "1+1 = 2"


def test_tool_runs_roundtrip_and_summary(db):
    sid = str(uuid.uuid4())
    _run(db.create_session_direct(sid, "cli:default:local"))
    _run(db.save_tool_runs([
        {
            "session_id": sid,
            "session_key": "cli:default:local",
            "turn_id": "turn-1",
            "tool_use_id": "call-1",
            "tool_name": "bash",
            "status": "success",
            "category": "",
            "duration": 0.25,
            "input_summary": '{"cmd": "pwd"}',
            "output_summary": "/tmp",
            "full_output": "/tmp",
            "output_truncated": False,
            "created_at": 1000.0,
        },
        {
            "session_id": sid,
            "session_key": "cli:default:local",
            "turn_id": "turn-1",
            "tool_use_id": "call-2",
            "tool_name": "write",
            "status": "denied",
            "category": "permission",
            "duration": 0.1,
            "input_summary": '{"path": "x"}',
            "output_summary": "blocked",
            "full_output": "blocked",
            "output_truncated": True,
            "error": "permission required",
            "permission_category": "write",
            "permission_decision": "ask",
            "required_allow": "write",
            "execution_mode": "sovereign",
            "created_at": 1001.0,
        },
    ]))

    recent = _run(db.recent_tool_runs(limit=10))
    filtered = _run(db.recent_tool_runs(limit=10, session_key="cli:default:local"))
    missing = _run(db.recent_tool_runs(limit=10, session_key="cli:missing:local"))
    detail = _run(db.get_tool_run(recent[1]["id"]))
    summary = _run(db.tool_run_summary(limit=10))

    assert [item["tool_name"] for item in recent] == ["bash", "write"]
    assert filtered == recent
    assert missing == []
    assert detail["tool_use_id"] == "call-2"
    assert detail["status"] == "denied"
    assert detail["output_truncated"] is True
    assert detail["permission_category"] == "write"
    assert summary["inspected"] == 2
    assert summary["tool_counts"] == {"bash": 1, "write": 1}
    assert summary["status_counts"] == {"denied": 1, "success": 1}
    assert summary["category_counts"] == {"permission": 1}
    assert summary["denied"] == 1
    assert summary["failed"] == 0
    assert summary["truncated"] == 1


def test_delete_cleans_messages(db):
    sid = str(uuid.uuid4())
    _run(db.create_session_direct(sid, "test:1:1"))
    _run(db.save_message(sid, "user", content="hello"))
    _run(db.save_tool_runs([{
        "session_id": sid,
        "session_key": "test:1:1",
        "tool_use_id": "call-1",
        "tool_name": "bash",
        "status": "success",
    }]))
    _run(db.delete_session(sid))

    history = _run(db.load_history(sid))
    tool_runs = _run(db.recent_tool_runs(limit=10, session_key="test:1:1"))
    assert len(history) == 0
    assert tool_runs == []
