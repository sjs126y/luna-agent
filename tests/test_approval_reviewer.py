from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


class FakeTransport:
    def __init__(self, text: str = '{"decision":"allow_once","reason":"ok"}', error: Exception | None = None):
        self.text = text
        self.error = error
        self.calls: list[dict] = []

    async def call(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.text)


def _agent(tmp_path: Path, transport: FakeTransport, *, risk: str = "medium"):
    from luna_agent.security.session import SecurityStateStore

    settings = SimpleNamespace(
        execution_mode="local-auto",
        sandbox_roots=[tmp_path],
        permission_grant_ttl_minutes=60,
    )
    security = SecurityStateStore(settings).context("review-session")
    return SimpleNamespace(
        _security_context=security,
        _security_grant_ttl_seconds=3600,
        _transport=transport,
        _provider=SimpleNamespace(model="current-model", api_mode="responses"),
        _approval_reviewer_config={
            "enabled": True,
            "model": "",
            "timeout_seconds": 2,
            "fallback": "human",
            "max_risk": risk,
        },
        _tool_calls_this_turn=0,
        _max_tool_calls_per_turn=20,
        _destructive_calls_this_turn=0,
        _max_destructive_per_turn=3,
        _interrupt_requested=False,
    )


@pytest.mark.asyncio
async def test_reviewer_uses_current_model_and_no_tools(tmp_path):
    from luna_agent.security.reviewer import ApprovalRequest, ApprovalReviewer

    transport = FakeTransport()
    reviewer = ApprovalReviewer(_agent(tmp_path, transport))
    result = await reviewer.review(ApprovalRequest(
        tool_name="demo",
        source="core",
        risk_level="low",
        mode="local-auto",
        reason="read a project file",
        input_summary="README.md",
    ))

    assert result.decision == "allow_once"
    assert result.model == "current-model"
    assert len(transport.calls) == 1
    assert transport.calls[0]["tools"] == []
    assert "demo" in transport.calls[0]["messages"][0]["content"][0]["text"]


@pytest.mark.asyncio
async def test_reviewer_failure_falls_back_to_human(tmp_path):
    from luna_agent.security.reviewer import ApprovalRequest, ApprovalReviewer

    reviewer = ApprovalReviewer(_agent(tmp_path, FakeTransport(error=RuntimeError("relay down"))))
    result = await reviewer.review(ApprovalRequest(
        tool_name="demo",
        source="core",
        risk_level="medium",
        mode="local-auto",
        reason="write an extra path",
        input_summary="/tmp/file.txt",
    ))

    assert result.decision == "ask_human"
    assert "RuntimeError" in result.error


@pytest.mark.asyncio
async def test_high_risk_never_reaches_reviewer(tmp_path):
    from luna_agent.security.reviewer import ApprovalRequest, ApprovalReviewer

    transport = FakeTransport()
    reviewer = ApprovalReviewer(_agent(tmp_path, transport, risk="medium"))
    result = await reviewer.review(ApprovalRequest(
        tool_name="worktree_merge",
        source="core",
        risk_level="high",
        mode="local-auto",
        reason="merge a worktree",
        input_summary="feature/test",
    ))

    assert result.decision == "ask_human"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_executor_uses_reviewer_then_runs_once(tmp_path):
    from luna_agent.tools.entry import ToolEntry
    from luna_agent.tools.executor import execute_tool_call_result
    from luna_agent.tools.registry import tool_registry

    calls = 0

    async def handler():
        nonlocal calls
        calls += 1
        return "ok"

    entry = ToolEntry("reviewed_prompt_demo", "demo", {}, handler, approval_mode="prompt")
    tool_registry.register(entry)
    transport = FakeTransport()
    agent = _agent(tmp_path, transport, risk="medium")
    confirmations = 0

    async def unexpected_confirm(_decision):
        nonlocal confirmations
        confirmations += 1
        return "deny"

    try:
        result = await execute_tool_call_result(
            {"id": "review-1", "name": entry.name, "input": {}},
            agent=agent,
            confirm=unexpected_confirm,
            approval_context="请读取并处理项目文件",
        )
    finally:
        tool_registry.unregister(entry.name)

    assert result.status == "success"
    assert calls == 1
    assert confirmations == 0
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_reviewer_deny_fallback_skips_human_confirmation(tmp_path):
    from luna_agent.tools.entry import ToolEntry
    from luna_agent.tools.executor import execute_tool_call_result
    from luna_agent.tools.registry import tool_registry

    async def handler():
        raise AssertionError("denied request must not run")

    entry = ToolEntry("reviewer_fallback_demo", "demo", {}, handler, approval_mode="prompt")
    tool_registry.register(entry)
    agent = _agent(tmp_path, FakeTransport(error=RuntimeError("unsupported model")))
    agent._approval_reviewer_config["fallback"] = "deny"
    confirmations = 0

    async def unexpected_confirm(_decision):
        nonlocal confirmations
        confirmations += 1
        return "allow"

    try:
        result = await execute_tool_call_result(
            {"id": "review-fallback", "name": entry.name, "input": {}},
            agent=agent,
            confirm=unexpected_confirm,
        )
    finally:
        tool_registry.unregister(entry.name)

    assert result.status == "denied"
    assert confirmations == 0
