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
    """Tool messages are stored but excluded from loaded history."""
    sid = str(uuid.uuid4())
    _run(db.create_session_direct(sid, "test:1:1"))
    _run(db.save_message(sid, "assistant", content="ok",
                         tool_calls=[{"id": "c1", "name": "calc", "input": {"expr": "1+1"}}],
                         tool_name="calc"))

    # History should NOT include tool messages
    history = _run(db.load_history(sid))
    assert len(history) == 0  # tool message filtered out


def test_tool_result_roundtrip(db):
    """Tool result messages are stored but excluded from loaded history."""
    sid = str(uuid.uuid4())
    _run(db.create_session_direct(sid, "test:1:1"))
    _run(db.save_message(sid, "user", content="2", tool_call_id="c1"))

    history = _run(db.load_history(sid))
    assert len(history) == 0  # tool message filtered out


def test_full_conversation_roundtrip(db):
    """Only text messages appear in loaded history, tool messages excluded."""
    sid = str(uuid.uuid4())
    _run(db.create_session_direct(sid, "test:1:1"))
    _run(db.save_message(sid, "user", content="what is 1+1?"))
    _run(db.save_message(sid, "assistant", content="",
                         tool_calls=[{"id": "c1", "name": "calc", "input": {"expr": "1+1"}}],
                         tool_name="calc"))
    _run(db.save_message(sid, "user", content="2", tool_call_id="c1"))
    _run(db.save_message(sid, "assistant", content="1+1 = 2"))

    # Only 2 text messages in history (user question + final answer)
    history = _run(db.load_history(sid))
    assert len(history) == 2
    assert history[0]["content"][0]["text"] == "what is 1+1?"
    assert history[1]["content"][0]["text"] == "1+1 = 2"


def test_delete_cleans_messages(db):
    sid = str(uuid.uuid4())
    _run(db.create_session_direct(sid, "test:1:1"))
    _run(db.save_message(sid, "user", content="hello"))
    _run(db.delete_session(sid))

    history = _run(db.load_history(sid))
    assert len(history) == 0
