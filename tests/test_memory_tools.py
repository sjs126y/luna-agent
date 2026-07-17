from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from personal_agent.memory.tools import memory_buffer_tool_entry, memory_tool_entry, set_memory_manager
from personal_agent.tools.runtime_context import reset_current_tool_agent, set_current_tool_agent


class Manager:
    def __init__(self):
        self.calls = []

    async def add_external(self, content, *, kind, session_key):
        self.calls.append(("add", content, kind, session_key))
        return SimpleNamespace(as_dict=lambda: {"provider": "fallback"})

    async def buffer_entries(self, *, status, session_key):
        self.calls.append(("buffer", status, session_key))
        return [{"observation_id": "o1"}]

    async def list_entries(self, *, target, session_key, limit):
        self.calls.append(("list", target, session_key, limit))
        return [
            {
                "id": f"m{index}",
                "content": "x" * 500,
                "kind": "fact",
                "importance": 0.7,
                "source_provider": "lumora",
                "created_at": "2026-07-17T00:00:00+00:00",
                "metadata": {"large": "ignored"},
                "scope": {"user_id": "private"},
            }
            for index in range(11)
        ]


@pytest.mark.asyncio
async def test_memory_tools_use_async_local_agent_session() -> None:
    manager = Manager()
    set_memory_manager(manager)
    token = set_current_tool_agent(SimpleNamespace(_memory_session_key="cli:work:u1"))
    try:
        added = await memory_tool_entry().handler(
            action="add", content="likes tea", kind="preference"
        )
        buffered = await memory_buffer_tool_entry().handler(action="list", status="pending")
    finally:
        reset_current_tool_agent(token)

    assert json.loads(added)["provider"] == "fallback"
    assert json.loads(buffered)[0]["observation_id"] == "o1"
    assert manager.calls == [
        ("add", "likes tea", "preference", "cli:work:u1"),
        ("buffer", "pending", "cli:work:u1"),
    ]


@pytest.mark.asyncio
async def test_memory_list_returns_compact_paginated_json() -> None:
    manager = Manager()
    set_memory_manager(manager)
    token = set_current_tool_agent(SimpleNamespace(_memory_session_key="wechat:user:user"))
    try:
        result = await memory_tool_entry().handler(action="list")
    finally:
        reset_current_tool_agent(token)

    payload = json.loads(result)
    assert payload["returned"] == 10
    assert payload["limit"] == 10
    assert payload["has_more"] is True
    assert len(payload["items"]) == 10
    assert payload["items"][0]["content_truncated"] is True
    assert len(payload["items"][0]["content"]) == 400
    assert "metadata" not in payload["items"][0]
    assert "scope" not in payload["items"][0]
    assert len(result) < 8000
    assert manager.calls == [("list", "external", "wechat:user:user", 11)]
