from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def clear_tracked_processes():
    from personal_agent.plugins.builtin.tools.builtin.process_tool import _processes

    _processes.clear()
    yield
    _processes.clear()


def test_activity_snapshot_normalizes_all_activity_sources(monkeypatch):
    from personal_agent import activity
    from personal_agent.plugins.builtin.tools.builtin import delegate
    from personal_agent.plugins.builtin.tools.builtin.process_tool import TrackedProcess, _processes

    monkeypatch.setattr(
        delegate,
        "list_active_agent_runs",
        lambda: [{
            "run_id": "active-agent",
            "status": "running",
            "role": "reviewer",
            "task": "check current work",
            "started_at": "2026-07-06T01:00:00Z",
            "duration": 9.5,
            "stop_requested": True,
        }],
    )
    monkeypatch.setattr(
        delegate,
        "list_agent_runs",
        lambda limit=None: [{
            "run_id": "failed-agent",
            "status": "error",
            "role": "tester",
            "task": "run tests",
            "started_at": "2026-07-06T00:00:00Z",
            "finished_at": "2026-07-06T00:00:05Z",
            "duration": 5.0,
            "error_message": "boom",
            "tool_calls": 2,
            "executed_tool_calls": 1,
            "denied_tool_calls": 1,
            "tool_results": 1,
        }],
    )

    started = time.time() - 12.0
    _processes[7] = TrackedProcess(
        pid=7,
        command="uv run pytest",
        proc=None,  # type: ignore[arg-type]
        started_at=started,
        status="running",
    )
    _processes[8] = TrackedProcess(
        pid=8,
        command="bad command",
        proc=None,  # type: ignore[arg-type]
        started_at=started,
        finished_at=started + 1.0,
        status="done",
        returncode=2,
        stderr="failed",
    )

    gateway = {
        "running_agents": 1,
        "stop_requested_agents": 1,
        "longest_running_seconds": 20.0,
        "running_agent_runs": [{
            "session_key": "telegram:c1:u1",
            "platform": "telegram",
            "chat_id": "c1",
            "user_id": "u1",
            "status": "running",
            "stop_requested": True,
            "duration_seconds": 20.0,
        }],
    }

    snapshot = activity.activity_snapshot(gateway_snapshot=gateway)

    assert snapshot["summary"]["active_total"] == 3
    assert snapshot["summary"]["has_active_work"] is True
    assert snapshot["summary"]["attention_required"] is True
    assert snapshot["summary"]["longest_running_seconds"] == 20.0
    assert snapshot["summary"]["counts"]["sub_agents"] == {
        "active": 1,
        "recent": 1,
        "failed_recent": 1,
        "stop_requested": 1,
    }
    assert snapshot["summary"]["counts"]["background_processes"]["running"] == 1
    assert snapshot["summary"]["counts"]["gateway_agents"] == {
        "running": 1,
        "stop_requested": 1,
    }
    assert snapshot["sub_agents"]["active_runs"][0]["status"] == "stopping"
    assert snapshot["sub_agents"]["recent_runs"][0]["status"] == "failed"
    assert snapshot["background_processes"]["items"][0]["kind"] == "background_process"
    assert snapshot["gateway_agents"]["running_agent_runs"][0]["status"] == "stopping"


def test_activity_detail_and_choices(monkeypatch):
    from personal_agent import activity
    from personal_agent.agents.runtime import AgentRun
    from personal_agent.plugins.builtin.tools.builtin import delegate
    from personal_agent.plugins.builtin.tools.builtin.process_tool import TrackedProcess, _processes

    run = AgentRun(
        run_id="agent-1",
        parent_turn_id="turn-1",
        status="completed",
        role="researcher",
        task="collect facts",
        started_at="2026-07-06T01:00:00Z",
        finished_at="2026-07-06T01:00:02Z",
        duration=2.0,
        result="done",
    )
    monkeypatch.setattr(delegate, "get_agent_run", lambda run_id: run if run_id == "agent-1" else None)
    monkeypatch.setattr(delegate, "list_active_agent_runs", lambda: [])
    monkeypatch.setattr(delegate, "list_agent_runs", lambda limit=None: [{
        "run_id": "agent-1",
        "status": "completed",
        "role": "researcher",
        "task": "collect facts",
    }])

    _processes[3] = TrackedProcess(
        pid=3,
        command="python worker.py",
        proc=None,  # type: ignore[arg-type]
        stdout="ready",
    )
    gateway = {
        "running_agent_runs": [{
            "session_key": "telegram:c1:u1",
            "platform": "telegram",
            "chat_id": "c1",
            "user_id": "u1",
            "status": "running",
        }],
    }

    agent_detail = activity.activity_detail("agents", "agent-1")
    process_detail = activity.activity_detail("processes", "3")
    gateway_detail = activity.activity_detail("gateway", "telegram:c1:u1", gateway_snapshot=gateway)

    assert agent_detail["kind"] == "sub_agent"
    assert agent_detail["run"]["result"] == "done"
    assert process_detail["kind"] == "background_process"
    assert process_detail["process"]["stdout"] == "ready"
    assert gateway_detail["kind"] == "gateway_agent"
    assert gateway_detail["gateway_run"]["id"] == "telegram:c1:u1"
    assert activity.activity_choices("activity_agents", query="facts")[0]["value"] == "agent-1"
    assert activity.activity_choices("activity_processes", query="worker")[0]["value"] == "3"
    assert (
        activity.activity_choices("activity_gateway", query="telegram", gateway_snapshot=gateway)[0]["value"]
        == "telegram:c1:u1"
    )
