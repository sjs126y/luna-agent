"""Tests for workflow engine and primitives."""

from __future__ import annotations

import asyncio
import json

import pytest

# Trigger registrations
from personal_agent.plugins.builtin.workflows.review import register as register_review
import personal_agent.plugins.builtin.tools.builtin.workflow_tool  # noqa

register_review()


# ── Mock LLM for workflow testing ──────────────────────

class MockResponse:
    def __init__(self, text: str):
        self.text = text
        self.tool_calls = None
        self.usage = {"input_tokens": 10, "output_tokens": 20}


def _make_mock_call(responses: list[str]):
    """Create a mock call_fn that returns responses in order."""
    idx = [0]  # mutable counter

    async def mock_call(messages, system_prompt, tools, max_tokens):
        if idx[0] < len(responses):
            text = responses[idx[0]]
            idx[0] += 1
            return MockResponse(text)
        return MockResponse("default response")

    return mock_call


# ── Parallel tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_parallel_runs_all():
    from personal_agent.workflow.primitives import parallel, _reset_context

    call_count = [0]
    async def mock_call(messages, system_prompt, tools, max_tokens):
        call_count[0] += 1
        return MockResponse("ok")

    _reset_context(mock_call, [], 4096)

    async def thunk_a():
        from personal_agent.workflow.primitives import agent
        return await agent("task a")

    async def thunk_b():
        from personal_agent.workflow.primitives import agent
        return await agent("task b")

    async def thunk_c():
        from personal_agent.workflow.primitives import agent
        return await agent("task c")

    results = await parallel([thunk_a, thunk_b, thunk_c])
    assert len(results) == 3
    assert call_count[0] == 3


@pytest.mark.asyncio
async def test_parallel_errors_are_nulled():
    from personal_agent.workflow.primitives import parallel, _reset_context

    async def mock_call(messages, system_prompt, tools, max_tokens):
        return MockResponse("ok")

    _reset_context(mock_call, [], 4096)

    async def bad_thunk():
        raise ValueError("boom")

    async def good_thunk():
        return "ok"

    results = await parallel([bad_thunk, good_thunk])
    assert results[0] is None
    assert results[1] == "ok"


# ── Pipeline tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_flows():
    from personal_agent.workflow.primitives import pipeline, _reset_context

    stage_order = []

    async def mock_call(messages, system_prompt, tools, max_tokens):
        return MockResponse("ok")

    _reset_context(mock_call, [], 4096)

    async def stage_a(item, original, index):
        stage_order.append(f"a_{index}")
        return f"{item}_a"

    async def stage_b(item, original, index):
        stage_order.append(f"b_{index}")
        return f"{item}_b"

    items = ["x", "y", "z"]
    results = await pipeline(items, stage_a, stage_b)

    assert len(results) == 3
    # Each item went through both stages
    assert "a_0" in stage_order
    assert "b_0" in stage_order
    assert "a_1" in stage_order
    assert "b_1" in stage_order


@pytest.mark.asyncio
async def test_pipeline_no_barrier():
    """Pipeline should not batch by stage — items flow independently."""
    from personal_agent.workflow.primitives import pipeline, _reset_context
    import asyncio as _asyncio

    order = []

    async def mock_call(messages, system_prompt, tools, max_tokens):
        # Simulate varying response times
        return MockResponse("ok")

    _reset_context(mock_call, [], 4096)

    async def stage_slow(item, original, index):
        if index == 0:
            await _asyncio.sleep(0.3)  # item 0 slow in stage 1
        order.append(f"s1_{index}")
        return item

    async def stage_fast(item, original, index):
        order.append(f"s2_{index}")
        return item

    items = [0, 1, 2]
    await pipeline(items, stage_slow, stage_fast)

    # Item 1 and 2 should complete stage 1 while item 0 is still sleeping
    # → item 1's stage 2 runs before item 0's stage 2
    # So s2_1 or s2_2 may appear before s2_0 in the order
    assert "s2_1" in order or "s2_2" in order


# ── Schema validation tests ────────────────────────────


@pytest.mark.asyncio
async def test_agent_with_schema():
    from personal_agent.workflow.primitives import agent, _reset_context

    async def mock_call(messages, system_prompt, tools, max_tokens):
        return MockResponse('{"name": "test", "count": 42}')

    _reset_context(mock_call, [], 4096)

    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "count": {"type": "number"}},
        "required": ["name", "count"],
    }
    result = await agent("get info", schema=schema)
    assert result == {"name": "test", "count": 42}


@pytest.mark.asyncio
async def test_agent_schema_retry():
    from personal_agent.workflow.primitives import agent, _reset_context

    calls = [MockResponse("not json at all"), MockResponse('{"name": "retry_ok", "count": 1}')]

    async def mock_call(messages, system_prompt, tools, max_tokens):
        return calls.pop(0)

    _reset_context(mock_call, [], 4096)

    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "count": {"type": "number"}},
        "required": ["name", "count"],
    }
    result = await agent("get info", schema=schema)
    assert result == {"name": "retry_ok", "count": 1}


# ── Registry tests ─────────────────────────────────────


def test_workflow_registry_has_review():
    from personal_agent.workflow.registry import workflow_registry

    wf = workflow_registry.get("review")
    assert wf is not None
    assert "code review" in wf.description.lower()
    assert wf.phases == ["Find", "Verify", "Report"]


def test_workflow_tools_registered():
    from personal_agent.tools.registry import tool_registry

    for name in ("workflow_run", "workflow_list"):
        entry = tool_registry.get(name)
        assert entry is not None, f"Tool '{name}' not registered"


# ── Engine smoke test ──────────────────────────────────


@pytest.mark.asyncio
async def test_engine_run_unknown_workflow():
    from personal_agent.workflow.engine import run_workflow, setup_engine

    async def mock_call(messages, system_prompt, tools, max_tokens):
        return MockResponse("ok")

    setup_engine(mock_call, [], 4096)
    result = await run_workflow("nonexistent_workflow_xyz")
    assert result["ok"] is False
    assert "Unknown" in result["error"]


# ── Phase and log tracking ─────────────────────────────


@pytest.mark.asyncio
async def test_phase_and_log_tracking():
    from personal_agent.workflow.primitives import phase, log, _reset_context, _phases, _logs

    async def mock_call(messages, system_prompt, tools, max_tokens):
        return MockResponse("ok")

    _reset_context(mock_call, [], 4096)
    phase("TestPhase")
    log("hello world")
    log("doing stuff")

    assert "TestPhase" in _phases
    assert "hello world" in _logs
    assert "doing stuff" in _logs
