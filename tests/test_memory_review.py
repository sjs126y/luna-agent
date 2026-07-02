"""Background memory review service."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from personal_agent.memory.review import MemoryReviewService


class Transport:
    def __init__(self, response=None, exc: Exception | None = None):
        self.response = response or SimpleNamespace(tool_calls=[])
        self.exc = exc
        self.calls = []

    async def call(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.response


class Agent:
    def __init__(self, transport):
        self._transport = transport
        self.tools = [{"name": "memory"}]


def _messages(count: int = 15) -> list[dict]:
    return [
        {"role": "user", "content": [{"type": "text", "text": f"m{i}"}]}
        for i in range(count)
    ]


def test_memory_review_maybe_spawn_gates_and_starts_thread(monkeypatch):
    started = []

    class Thread:
        def __init__(self, *, target, daemon, name):
            self.target = target
            self.daemon = daemon
            self.name = name

        def start(self):
            started.append((self.daemon, self.name))

    monkeypatch.setattr("threading.Thread", Thread)
    service = MemoryReviewService()

    assert service.maybe_spawn(
        agent=None,
        messages=[],
        should_review=True,
        final_response="ok",
    ) is False
    assert service.maybe_spawn(
        agent=object(),
        messages=[],
        should_review=False,
        final_response="ok",
    ) is False
    assert service.maybe_spawn(
        agent=object(),
        messages=[],
        should_review=True,
        final_response="",
    ) is False
    assert service.maybe_spawn(
        agent=object(),
        messages=[],
        should_review=True,
        final_response="ok",
    ) is True
    assert started == [(True, "mem-review")]


@pytest.mark.asyncio
async def test_memory_review_calls_transport_with_recent_messages_and_tools(monkeypatch):
    tool_calls = [{"id": "call-1", "name": "memory"}]
    transport = Transport(SimpleNamespace(tool_calls=tool_calls))
    agent = Agent(transport)
    executed = []

    async def execute_tool_calls(calls, messages, *, agent):
        executed.append((calls, messages, agent))

    monkeypatch.setattr("personal_agent.tools.executor.execute_tool_calls", execute_tool_calls)

    await MemoryReviewService().review(agent=agent, messages=_messages(15))

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert len(call["messages"]) == 13
    assert call["messages"][0]["content"][0]["text"] == "m3"
    assert "Review this conversation" in call["messages"][-1]["content"][0]["text"]
    assert call["system_prompt"] == "你是一个记忆管理助手。判断对话中是否有值得保存的信息。"
    assert call["tools"] == agent.tools
    assert call["max_tokens"] == 512
    assert executed == [(tool_calls, call["messages"], agent)]


@pytest.mark.asyncio
async def test_memory_review_swallows_transport_errors():
    agent = Agent(Transport(exc=RuntimeError("boom")))

    await MemoryReviewService().review(agent=agent, messages=_messages(1))
