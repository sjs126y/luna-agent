"""Controlled multi-agent runtime."""

import asyncio

import pytest

from personal_agent.agents.runtime import AgentRuntime, AgentSpec
from personal_agent.models.messages import NormalizedResponse


@pytest.mark.asyncio
async def test_agent_runtime_defaults_to_readonly_tools():
    seen_tool_names = []

    async def call_fn(messages, system_prompt, tools, max_tokens):
        seen_tool_names.append([tool["name"] for tool in tools])
        return NormalizedResponse(
            text="done",
            usage={"input_tokens": 3, "output_tokens": 2},
        )

    runtime = AgentRuntime(
        call_fn=call_fn,
        tools=[
            {"name": "read", "description": "read", "input_schema": {}},
            {"name": "write", "description": "write", "input_schema": {}},
            {"name": "delegate_task", "description": "delegate", "input_schema": {}},
        ],
        max_tokens=100,
    )

    run = await runtime.run("inspect safely", AgentSpec(role="reviewer"))

    assert run.status == "completed"
    assert run.role == "reviewer"
    assert run.task == "inspect safely"
    assert run.tool_policy == "readonly"
    assert run.result == "done"
    assert run.usage == {"input_tokens": 3, "output_tokens": 2}
    assert run.granted_tools == ["read"]
    assert [item["name"] for item in run.denied_tools] == ["write", "delegate_task"]
    assert seen_tool_names == [["read"]]


@pytest.mark.asyncio
async def test_agent_runtime_blocks_destructive_even_with_all_policy():
    seen_tool_names = []

    async def call_fn(messages, system_prompt, tools, max_tokens):
        seen_tool_names.append([tool["name"] for tool in tools])
        return NormalizedResponse(text="done")

    runtime = AgentRuntime(
        call_fn=call_fn,
        tools=[
            {"name": "read", "description": "read", "input_schema": {}},
            {"name": "bash", "description": "bash", "input_schema": {}},
        ],
        max_tokens=100,
    )

    run = await runtime.run("inspect safely", AgentSpec(role="reviewer", tool_policy="all"))

    assert run.status == "completed"
    assert seen_tool_names == [["read"]]
    assert run.granted_tools == ["read"]
    assert run.denied_tools == [{
        "name": "bash",
        "allowed": False,
        "reason": "destructive tool requires explicit sub-agent authorization",
        "phase": "selection",
    }]


@pytest.mark.asyncio
async def test_agent_runtime_allows_destructive_when_explicitly_authorized():
    seen_tool_names = []

    async def call_fn(messages, system_prompt, tools, max_tokens):
        seen_tool_names.append([tool["name"] for tool in tools])
        return NormalizedResponse(text="done")

    runtime = AgentRuntime(
        call_fn=call_fn,
        tools=[
            {"name": "read", "description": "read", "input_schema": {}},
            {"name": "bash", "description": "bash", "input_schema": {}},
        ],
        max_tokens=100,
    )

    run = await runtime.run(
        "inspect with shell",
        AgentSpec(role="operator", tool_policy=["read", "bash"]),
        allow_destructive=True,
    )

    assert run.status == "completed"
    assert seen_tool_names == [["read", "bash"]]
    assert run.granted_tools == ["read", "bash"]
    assert run.denied_tools == []


@pytest.mark.asyncio
async def test_agent_runtime_allowlist_only_grants_named_tools():
    seen_tool_names = []

    async def call_fn(messages, system_prompt, tools, max_tokens):
        seen_tool_names.append([tool["name"] for tool in tools])
        return NormalizedResponse(text="done")

    runtime = AgentRuntime(
        call_fn=call_fn,
        tools=[
            {"name": "read", "description": "read", "input_schema": {}},
            {"name": "grep", "description": "grep", "input_schema": {}},
            {"name": "calculator", "description": "calculator", "input_schema": {}},
        ],
        max_tokens=100,
    )

    run = await runtime.run(
        "inspect with grep only",
        AgentSpec(role="reviewer", tool_policy="allowlist", allowed_tools=["grep"]),
    )

    assert run.status == "completed"
    assert seen_tool_names == [["grep"]]
    assert run.granted_tools == ["grep"]
    assert [item["name"] for item in run.denied_tools] == ["read", "calculator"]


@pytest.mark.asyncio
async def test_agent_runtime_denies_ungranted_tool_call_and_continues():
    call_count = 0
    final_messages = []

    async def call_fn(messages, system_prompt, tools, max_tokens):
        nonlocal call_count, final_messages
        call_count += 1
        if call_count == 1:
            return NormalizedResponse(
                text="need write",
                tool_calls=[{
                    "id": "toolu_1",
                    "name": "write",
                    "input": {"path": "x.txt", "content": "no"},
                }],
            )
        final_messages = messages
        return NormalizedResponse(text="handled denied tool")

    runtime = AgentRuntime(
        call_fn=call_fn,
        tools=[
            {"name": "read", "description": "read", "input_schema": {}},
            {"name": "write", "description": "write", "input_schema": {}},
        ],
        max_tokens=100,
    )

    run = await runtime.run("try unsafe tool", AgentSpec(role="assistant"))

    assert run.status == "completed"
    assert run.result == "handled denied tool"
    assert run.executed_tool_calls == []
    assert run.denied_tool_calls == [{
        "call_id": "toolu_1",
        "name": "write",
        "allowed": False,
        "reason": "tool was not granted to this sub-agent",
    }]
    assert "denied" in final_messages[-2]["content"][0]["content"]


@pytest.mark.asyncio
async def test_agent_runtime_retries_schema_output():
    calls = 0

    async def call_fn(messages, system_prompt, tools, max_tokens):
        nonlocal calls
        calls += 1
        if calls == 1:
            return NormalizedResponse(text="not json", usage={"input_tokens": 1, "output_tokens": 1})
        return NormalizedResponse(text='{"answer": "ok"}', usage={"input_tokens": 2, "output_tokens": 3})

    runtime = AgentRuntime(call_fn=call_fn, tools=[], max_tokens=100)
    run = await runtime.run(
        "return json",
        AgentSpec(
            role="formatter",
            tool_policy="none",
            output_schema={"type": "object", "required": ["answer"]},
        ),
    )

    assert run.status == "completed"
    assert '"answer": "ok"' in run.result
    assert calls == 2
    assert run.usage == {"input_tokens": 3, "output_tokens": 4}


@pytest.mark.asyncio
async def test_agent_runtime_records_success_and_error_runs():
    async def ok_call(messages, system_prompt, tools, max_tokens):
        return NormalizedResponse(text="ok")

    runtime = AgentRuntime(call_fn=ok_call, tools=[], max_tokens=100)
    ok = await runtime.run("ok", AgentSpec(role="assistant"))

    async def bad_call(messages, system_prompt, tools, max_tokens):
        raise RuntimeError("boom")

    runtime.call_fn = bad_call
    bad = await runtime.run("bad", AgentSpec(role="assistant"))

    runs = runtime.list_runs()
    assert [run.run_id for run in runs] == [ok.run_id, bad.run_id]
    assert [run.status for run in runs] == ["completed", "error"]
    assert runtime.get_run(ok.run_id) is ok


@pytest.mark.asyncio
async def test_agent_runtime_records_uninitialized_and_caps_history():
    runtime = AgentRuntime(history_limit=2)

    first = await runtime.run("first", AgentSpec(role="assistant"))

    async def call_fn(messages, system_prompt, tools, max_tokens):
        return NormalizedResponse(text=messages[0]["content"][0]["text"])

    runtime.call_fn = call_fn
    second = await runtime.run("second", AgentSpec(role="assistant"))
    third = await runtime.run("third", AgentSpec(role="assistant"))

    assert first.status == "error"
    assert [run.run_id for run in runtime.list_runs()] == [second.run_id, third.run_id]
    assert runtime.get_run(first.run_id) is None


@pytest.mark.asyncio
async def test_agent_runtime_parallel_runs_keep_distinct_messages():
    async def call_fn(messages, system_prompt, tools, max_tokens):
        await asyncio.sleep(0)
        return NormalizedResponse(text=messages[0]["content"][0]["text"])

    runtime = AgentRuntime(call_fn=call_fn, tools=[], max_tokens=100)
    first, second = await asyncio.gather(
        runtime.run("one", AgentSpec(role="assistant")),
        runtime.run("two", AgentSpec(role="assistant")),
    )

    assert first.messages is not second.messages
    assert first.result == "one"
    assert second.result == "two"
    assert len(runtime.list_runs()) == 2


@pytest.mark.asyncio
async def test_agent_runtime_persists_runs_to_jsonl(tmp_path):
    async def call_fn(messages, system_prompt, tools, max_tokens):
        return NormalizedResponse(text="persisted", usage={"input_tokens": 1, "output_tokens": 2})

    path = tmp_path / "agent_runs.jsonl"
    runtime = AgentRuntime(call_fn=call_fn, tools=[], max_tokens=100, run_store_path=path)
    run = await runtime.run("persist me", AgentSpec(role="assistant"))

    loaded = AgentRuntime(run_store_path=path)

    assert path.exists()
    assert loaded.get_run(run.run_id).result == "persisted"
    assert loaded.get_run(run.run_id).task == "persist me"
    loaded.clear_runs()
    assert not path.exists()
