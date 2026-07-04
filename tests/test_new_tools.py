"""Tests for new tools: clarify, process, execute_code, delegate."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


# ── clarify ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clarify_question_only():
    from personal_agent.plugins.builtin.tools.builtin.clarify import _clarify
    import json

    q = json.dumps([{
        "header": "Continue",
        "question": "Do you want to continue?",
        "options": [],
    }])
    result = await _clarify(q)
    assert "Do you want to continue?" in result
    assert "Other" in result


@pytest.mark.asyncio
async def test_clarify_with_choices():
    from personal_agent.plugins.builtin.tools.builtin.clarify import _clarify
    import json

    q = json.dumps([{
        "header": "Language",
        "question": "What language?",
        "options": [
            {"label": "Python", "description": "Great for AI"},
            {"label": "Rust", "description": "Fast and safe"},
        ],
    }])
    result = await _clarify(q)
    assert "1. **Python**" in result
    assert "2. **Rust**" in result


@pytest.mark.asyncio
async def test_clarify_multi_question():
    from personal_agent.plugins.builtin.tools.builtin.clarify import _clarify
    import json

    q = json.dumps([
        {"header": "A", "question": "First?", "options": [{"label": "X", "description": ""}]},
        {"header": "B", "question": "Second?", "options": [{"label": "Y", "description": ""}]},
    ])
    result = await _clarify(q)
    assert "## A" in result
    assert "## B" in result
    assert "---" in result


@pytest.mark.asyncio
async def test_clarify_multi_select():
    from personal_agent.plugins.builtin.tools.builtin.clarify import _clarify
    import json

    q = json.dumps([{
        "header": "Features",
        "question": "Which features?",
        "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}],
        "multiSelect": True,
    }])
    result = await _clarify(q)
    assert "multiple options" in result.lower()


@pytest.mark.asyncio
async def test_clarify_invalid_json():
    from personal_agent.plugins.builtin.tools.builtin.clarify import _clarify

    result = await _clarify("not json")
    assert "Error" in result
    assert "invalid" in result.lower()


# ── process ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_list_empty():
    from personal_agent.plugins.builtin.tools.builtin.process_tool import _process_list

    result = await _process_list()
    assert "No background processes" in result or "running" not in result.lower()


@pytest.mark.asyncio
async def test_process_kill_nonexistent():
    from personal_agent.plugins.builtin.tools.builtin.process_tool import _process_kill

    result = await _process_kill(99999)
    assert "no process" in result.lower()


@pytest.mark.asyncio
async def test_process_wait_nonexistent():
    from personal_agent.plugins.builtin.tools.builtin.process_tool import _process_wait

    result = await _process_wait(99999)
    assert "no process" in result.lower()


@pytest.mark.asyncio
async def test_process_start_read_wait(tmp_path: Path):
    from personal_agent.plugins.builtin.tools.builtin.bash import set_work_dir
    from personal_agent.plugins.builtin.tools.builtin.process_tool import (
        _process_read,
        _process_start,
        _process_wait,
    )
    from personal_agent.tools.sandbox import init_sandbox

    init_sandbox([tmp_path], [])
    set_work_dir(tmp_path)

    result = await _process_start(
        'python -u -c "import sys\nprint(\'out\')\nprint(\'err\', file=sys.stderr)"',
        cwd=str(tmp_path),
    )
    assert "Process [" in result
    pid = int(result.split("Process [", 1)[1].split("]", 1)[0])

    waited = await _process_wait(pid, timeout=5)
    assert "exit_code: 0" in waited
    assert "out" in waited
    assert "err" in waited

    read = await _process_read(pid)
    assert "stdout:" in read
    assert "stderr:" in read
    assert "out" in read


@pytest.mark.asyncio
async def test_process_start_reuses_bash_sandbox_precheck(tmp_path: Path):
    from personal_agent.plugins.builtin.tools.builtin.bash import set_work_dir
    from personal_agent.plugins.builtin.tools.builtin.process_tool import _process_start
    from personal_agent.tools.sandbox import init_sandbox

    init_sandbox([tmp_path], [])
    set_work_dir(tmp_path)

    result = await _process_start("rm -rf /", cwd=str(tmp_path))
    assert "hard blacklist" in result.lower()


@pytest.mark.asyncio
async def test_process_start_blocks_sandbox_patterns_and_bad_cwd(tmp_path: Path):
    from personal_agent.plugins.builtin.tools.builtin.bash import set_work_dir
    from personal_agent.plugins.builtin.tools.builtin.process_tool import _process_start
    from personal_agent.tools.sandbox import init_sandbox

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / ".env").write_text("SECRET=1", encoding="utf-8")

    init_sandbox([workspace], ["**/.env"])
    set_work_dir(workspace)

    blocked_path = await _process_start("cat .env", cwd=str(workspace))
    bad_cwd = await _process_start("pwd", cwd=str(outside))

    assert "sandbox blocked" in blocked_path.lower() or "path blocked" in blocked_path.lower()
    assert "outside sandbox roots" in bad_cwd.lower()


@pytest.mark.asyncio
async def test_process_start_blocks_network_when_policy_denies(tmp_path: Path):
    from personal_agent.plugins.builtin.tools.builtin.bash import set_allow_network, set_work_dir
    from personal_agent.plugins.builtin.tools.builtin.process_tool import _process_start
    from personal_agent.tools.sandbox import init_sandbox

    init_sandbox([tmp_path], [])
    set_work_dir(tmp_path)
    set_allow_network(False)

    result = await _process_start("curl https://example.com", cwd=str(tmp_path))

    assert "network" in result.lower()


@pytest.mark.asyncio
async def test_process_read_invalid_stream():
    from personal_agent.plugins.builtin.tools.builtin.process_tool import _process_read

    result = await _process_read(99999, stream="wat")
    assert "no process" in result.lower()


@pytest.mark.asyncio
async def test_process_read_since_last_and_tail_modes(tmp_path: Path):
    from personal_agent.plugins.builtin.tools.builtin.bash import set_work_dir
    from personal_agent.plugins.builtin.tools.builtin.process_tool import (
        _process_read,
        _process_start,
        _process_wait,
    )
    from personal_agent.tools.sandbox import init_sandbox

    init_sandbox([tmp_path], [])
    set_work_dir(tmp_path)

    result = await _process_start(
        'python -u -c "print(\'first\')\nprint(\'second\')"',
        cwd=str(tmp_path),
    )
    pid = int(result.split("Process [", 1)[1].split("]", 1)[0])
    await _process_wait(pid, timeout=5)

    first = await _process_read(pid, stream="stdout", mode="since_last")
    second = await _process_read(pid, stream="stdout", mode="since_last")
    tail = await _process_read(pid, stream="stdout", mode="tail", tail_chars=6)
    all_output = await _process_read(pid, stream="stdout", mode="all")

    assert "first" in first
    assert "second" in first
    assert "(empty)" in second
    assert "second" in tail
    assert "first" in all_output


@pytest.mark.asyncio
async def test_process_read_invalid_mode():
    from personal_agent.plugins.builtin.tools.builtin.process_tool import TrackedProcess, _process_read, _processes

    _processes[98765] = TrackedProcess(pid=98765, command="demo", proc=None)  # type: ignore[arg-type]
    try:
        result = await _process_read(98765, mode="wat")
    finally:
        _processes.pop(98765, None)
    assert "mode must be one of" in result


def test_process_read_truncation_flags():
    from personal_agent.plugins.builtin.tools.builtin.process_tool import (
        TrackedProcess,
        _append_output,
        _format_output,
    )

    process = TrackedProcess(pid=123, command="demo", proc=None)  # type: ignore[arg-type]
    _append_output(process, "stdout", "x" * 5000)
    result = _format_output(process, stream="stdout", tail_chars=20, mode="all", header="output")

    assert process.stdout_truncated is True
    assert "stdout_truncated: true" in result


@pytest.mark.asyncio
async def test_process_list_filter_and_limit(tmp_path: Path):
    from personal_agent.plugins.builtin.tools.builtin.bash import set_work_dir
    from personal_agent.plugins.builtin.tools.builtin.process_tool import (
        _process_clear,
        _process_kill,
        _process_list,
        _process_start,
        _process_wait,
    )
    from personal_agent.tools.sandbox import init_sandbox

    init_sandbox([tmp_path], [])
    set_work_dir(tmp_path)

    done_result = await _process_start('python -u -c "print(\'done\')"', cwd=str(tmp_path))
    done_pid = int(done_result.split("Process [", 1)[1].split("]", 1)[0])
    await _process_wait(done_pid, timeout=5)

    running_result = await _process_start('python -u -c "import time\ntime.sleep(20)"', cwd=str(tmp_path))
    running_pid = int(running_result.split("Process [", 1)[1].split("]", 1)[0])

    done_list = await _process_list(status="done", limit=1)
    running_list = await _process_list(status="running")

    assert f"[{done_pid}]" in done_list
    assert f"[{running_pid}]" in running_list

    await _process_kill(running_pid)
    await _process_clear(pid=done_pid)
    await _process_clear(pid=running_pid)


@pytest.mark.asyncio
async def test_process_clear_finished_and_refuses_running(tmp_path: Path):
    from personal_agent.plugins.builtin.tools.builtin.bash import set_work_dir
    from personal_agent.plugins.builtin.tools.builtin.process_tool import (
        _process_clear,
        _process_list,
        _process_start,
        _process_wait,
    )
    from personal_agent.tools.sandbox import init_sandbox

    init_sandbox([tmp_path], [])
    set_work_dir(tmp_path)

    done_result = await _process_start('python -u -c "print(\'done\')"', cwd=str(tmp_path))
    done_pid = int(done_result.split("Process [", 1)[1].split("]", 1)[0])
    await _process_wait(done_pid, timeout=5)

    running_result = await _process_start('python -u -c "import time\ntime.sleep(20)"', cwd=str(tmp_path))
    running_pid = int(running_result.split("Process [", 1)[1].split("]", 1)[0])

    refused = await _process_clear(pid=running_pid)
    cleared = await _process_clear(status="finished")
    remaining = await _process_list(status="running")

    assert "still running" in refused
    assert "Cleared" in cleared
    assert f"[{done_pid}]" not in remaining
    assert f"[{running_pid}]" in remaining

    from personal_agent.plugins.builtin.tools.builtin.process_tool import _process_kill

    await _process_kill(running_pid)
    await _process_clear(pid=running_pid)


@pytest.mark.asyncio
async def test_process_lifecycle():
    """Spawn a real background process, list it, wait for it, verify."""
    from personal_agent.plugins.builtin.tools.builtin.process_tool import (
        _process_list, _process_wait, _process_kill, _register,
    )

    # Spawn a real background process
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(0.3); print('done')",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    pid = _register(proc, "echo test")

    # List — should appear
    result = await _process_list()
    assert str(pid) in result
    assert "echo test" in result

    # Wait for it
    result = await _process_wait(pid, timeout=5)
    assert "rc=0" in result or "finished" in result.lower()

    # Kill after completion — should say already finished
    result = await _process_kill(pid)
    assert "already finished" in result.lower()


# ── execute_code ────────────────────────────────────────


@pytest.mark.asyncio
async def test_bash_structured_output(tmp_path: Path):
    from personal_agent.plugins.builtin.tools.builtin.bash import _bash, set_work_dir
    from personal_agent.tools.sandbox import init_sandbox

    init_sandbox([tmp_path], [])
    set_work_dir(tmp_path)

    result = await _bash('python -u -c "print(\'hello\')"', timeout=5)

    assert "Command finished" in result
    assert "exit_code: 0" in result
    assert "duration:" in result
    assert "stdout:" in result
    assert "hello" in result
    assert "stderr:" in result


@pytest.mark.asyncio
async def test_bash_timeout_suggests_background(tmp_path: Path):
    from personal_agent.plugins.builtin.tools.builtin.bash import _bash, set_work_dir
    from personal_agent.tools.sandbox import init_sandbox

    init_sandbox([tmp_path], [])
    set_work_dir(tmp_path)

    result = await _bash('python -u -c "import time\ntime.sleep(2)"', timeout=1)

    assert "Command timed out" in result
    assert "process_start" in result
    assert "exit_code:" in result


# ── file edit/write reliability ────────────────────────


@pytest.mark.asyncio
async def test_file_edit_rejects_empty_append(tmp_path: Path):
    from personal_agent.plugins.builtin.tools.builtin.file_edit import _file_edit
    from personal_agent.tools.sandbox import init_sandbox

    init_sandbox([tmp_path], [])

    result = await _file_edit("append", "notes.md", content="")

    assert "cannot be empty" in result.lower()


@pytest.mark.asyncio
async def test_file_edit_replace_reports_occurrences(tmp_path: Path):
    from personal_agent.plugins.builtin.tools.builtin.file_edit import _file_edit
    from personal_agent.tools.sandbox import init_sandbox

    path = tmp_path / "notes.md"
    path.write_text("alpha beta beta", encoding="utf-8")
    init_sandbox([tmp_path], [])

    empty_old = await _file_edit("replace", str(path), old_text="", new_text="x")
    missing = await _file_edit("replace", str(path), old_text="gamma", new_text="x")
    replaced = await _file_edit("replace", str(path), old_text="beta", new_text="BETA")

    assert "old_text cannot be empty" in empty_old
    assert "occurrences=0" in missing
    assert "Replaced 1 of 2 occurrences" in replaced
    assert path.read_text(encoding="utf-8") == "alpha BETA beta"


@pytest.mark.asyncio
async def test_file_edit_size_limit_and_sandbox_block(tmp_path: Path, monkeypatch):
    from personal_agent.plugins.builtin.tools.builtin import file_edit
    from personal_agent.tools.sandbox import init_sandbox

    path = tmp_path / "notes.md"
    path.write_text("12345", encoding="utf-8")
    init_sandbox([tmp_path], ["**/.env"])

    monkeypatch.setattr(file_edit, "_MAX_WRITE_BYTES", 6)
    too_large = await file_edit._file_edit("append", str(path), content="789")
    blocked = await file_edit._file_edit("append", ".env", content="x")

    assert "exceed max size" in too_large.lower()
    assert "path blocked" in blocked.lower()


@pytest.mark.asyncio
async def test_execute_code_basic():
    from personal_agent.plugins.builtin.tools.builtin.execute_code import _execute_code

    result = await _execute_code("print('hello world')")
    assert "hello world" in result


@pytest.mark.asyncio
async def test_execute_code_math():
    from personal_agent.plugins.builtin.tools.builtin.execute_code import _execute_code

    result = await _execute_code("print(2 ** 10)")
    assert "1024" in result


@pytest.mark.asyncio
async def test_execute_code_stderr():
    from personal_agent.plugins.builtin.tools.builtin.execute_code import _execute_code

    result = await _execute_code("import sys; print('ok', file=sys.stderr)")
    assert "[stderr]" in result
    assert "ok" in result


@pytest.mark.asyncio
async def test_execute_code_exception():
    from personal_agent.plugins.builtin.tools.builtin.execute_code import _execute_code

    result = await _execute_code("raise RuntimeError('boom')")
    assert "RuntimeError" in result
    assert "boom" in result


@pytest.mark.asyncio
async def test_execute_code_imports():
    from personal_agent.plugins.builtin.tools.builtin.execute_code import _execute_code

    result = await _execute_code(
        "import json, math, datetime, collections; "
        "print(json.dumps({'sqrt': math.sqrt(16), 'now': str(datetime.date.today())}))"
    )
    assert "4.0" in result


@pytest.mark.asyncio
async def test_execute_code_timeout():
    from personal_agent.plugins.builtin.tools.builtin.execute_code import _execute_code

    result = await _execute_code("import time; time.sleep(120)", timeout=5)
    assert "timed out" in result.lower()


@pytest.mark.asyncio
async def test_execute_code_sandbox_env():
    """API keys should NOT be available in the sandbox."""
    from personal_agent.plugins.builtin.tools.builtin.execute_code import _execute_code

    result = await _execute_code(
        "import os; print('LLM_API_KEY' in os.environ)"
    )
    assert "False" in result


@pytest.mark.asyncio
async def test_execute_code_isolated_cwd():
    """Sandbox should run in a temp directory, not the agent's directory."""
    from personal_agent.plugins.builtin.tools.builtin.execute_code import _execute_code

    result = await _execute_code("import os; print(os.getcwd())")
    # Should be a temp dir, not the project dir
    assert "Personal Agent" not in result
    assert "Temp" in result or "tmp" in result.lower()


@pytest.mark.asyncio
async def test_execute_code_no_output():
    from personal_agent.plugins.builtin.tools.builtin.execute_code import _execute_code

    result = await _execute_code("x = 1 + 1")
    assert "no output" in result.lower()


# ── delegate_task ───────────────────────────────────────


@pytest.mark.asyncio
async def test_sub_agent_not_initialized():
    """Without setup, sub_agent should return a clear error."""
    from personal_agent.plugins.builtin.tools.builtin.delegate import _sub_agent, reset_delegate

    reset_delegate()
    result = await _sub_agent("test prompt")
    assert "not initialized" in result.lower()


@pytest.mark.asyncio
async def test_sub_parallel_bad_json():
    from personal_agent.plugins.builtin.tools.builtin.delegate import _sub_parallel

    result = await _sub_parallel("not json")
    assert "invalid" in result.lower()


@pytest.mark.asyncio
async def test_sub_agent_uses_runtime_after_setup():
    from personal_agent.models.messages import NormalizedResponse
    from personal_agent.plugins.builtin.tools.builtin.delegate import _sub_agent, setup_delegate

    seen = {}

    async def call_fn(messages, system_prompt, tools, max_tokens):
        seen["tools"] = [tool["name"] for tool in tools]
        return NormalizedResponse(text="runtime-ok")

    setup_delegate(
        call_fn,
        tools=[
            {"name": "read", "description": "read", "input_schema": {}},
            {"name": "write", "description": "write", "input_schema": {}},
        ],
        max_tokens=100,
    )

    result = await _sub_agent("inspect")

    assert result == "runtime-ok"
    assert seen["tools"] == ["read"]


@pytest.mark.asyncio
async def test_sub_agent_accepts_allowed_tools_json_string():
    from personal_agent.models.messages import NormalizedResponse
    from personal_agent.plugins.builtin.tools.builtin.delegate import (
        _sub_agent,
        list_agent_runs,
        setup_delegate,
    )

    seen = {}

    async def call_fn(messages, system_prompt, tools, max_tokens):
        seen["tools"] = [tool["name"] for tool in tools]
        return NormalizedResponse(text="runtime-ok")

    setup_delegate(
        call_fn,
        tools=[
            {"name": "read", "description": "read", "input_schema": {}},
            {"name": "write", "description": "write", "input_schema": {}},
        ],
        max_tokens=100,
    )

    result = await _sub_agent("inspect", allowed_tools='["write"]')

    assert result == "runtime-ok"
    assert seen["tools"] == ["read"]
    assert list_agent_runs()[0]["denied_tools"] == 1


@pytest.mark.asyncio
async def test_delegate_task_allowlist_only_grants_named_tools():
    from personal_agent.models.messages import NormalizedResponse
    from personal_agent.plugins.builtin.tools.builtin.delegate import _delegate_task, setup_delegate

    seen = {}

    async def call_fn(messages, system_prompt, tools, max_tokens):
        seen["tools"] = [tool["name"] for tool in tools]
        return NormalizedResponse(text="allowlist-ok")

    setup_delegate(
        call_fn,
        tools=[
            {"name": "read", "description": "read", "input_schema": {}},
            {"name": "grep", "description": "grep", "input_schema": {}},
            {"name": "calculator", "description": "calculator", "input_schema": {}},
        ],
        max_tokens=100,
    )

    result = await _delegate_task(
        "inspect",
        tool_policy="allowlist",
        allowed_tools='["grep"]',
    )

    assert "allowlist-ok" in result
    assert seen["tools"] == ["grep"]


@pytest.mark.asyncio
async def test_delegate_records_denied_tool_calls_in_detail():
    from personal_agent.models.messages import NormalizedResponse
    from personal_agent.plugins.builtin.tools.builtin.delegate import (
        _delegate_task,
        format_agent_run,
        list_agent_runs,
        setup_delegate,
    )

    calls = 0

    async def call_fn(messages, system_prompt, tools, max_tokens):
        nonlocal calls
        calls += 1
        if calls == 1:
            return NormalizedResponse(
                text="need write",
                tool_calls=[{
                    "id": "toolu_1",
                    "name": "write",
                    "input": {"path": "x.txt", "content": "no"},
                }],
            )
        return NormalizedResponse(text="denied-ok")

    setup_delegate(
        call_fn,
        tools=[
            {"name": "read", "description": "read", "input_schema": {}},
            {"name": "write", "description": "write", "input_schema": {}},
        ],
        max_tokens=100,
    )

    result = await _delegate_task("try write")
    summary = list_agent_runs()[0]
    run_id = summary["run_id"]
    detail = format_agent_run(run_id)

    assert "denied-ok" in result
    assert "denied=1" in result
    assert summary["denial_categories"] == {"destructive": 2}
    assert summary["denied_tool_call_details"][0]["name"] == "write"
    assert summary["denied_tool_call_details"][0]["phase"] == "call"
    assert summary["tool_result_summaries"][0]["denied"] is True
    assert "状态: completed (已完成)" in detail
    assert "配额: tokens=0/100" in detail
    assert "错误类型: -" in detail
    assert "拒绝工具调用: 1" in detail
    assert "工具结果摘要: 1" in detail
    assert "category=destructive" in detail
    assert "phase=call" in detail
    assert "write" in detail


@pytest.mark.asyncio
async def test_delegate_lists_agent_run_summaries():
    from personal_agent.models.messages import NormalizedResponse
    from personal_agent.plugins.builtin.tools.builtin.delegate import (
        _delegate_task,
        clear_agent_runs,
        format_agent_run,
        format_agent_runs,
        list_agent_runs,
        setup_delegate,
    )

    async def call_fn(messages, system_prompt, tools, max_tokens):
        return NormalizedResponse(text="summary-ok", usage={"input_tokens": 1, "output_tokens": 2})

    setup_delegate(call_fn, tools=[], max_tokens=100)

    result = await _delegate_task("summarize")
    runs = list_agent_runs()

    assert "summary-ok" in result
    assert len(runs) == 1
    assert runs[0]["role"] == "assistant"
    assert runs[0]["task"] == "summarize"
    assert runs[0]["status"] == "completed"
    assert runs[0]["usage"] == {"input_tokens": 1, "output_tokens": 2}
    assert runs[0]["schema_version"] == 3
    assert runs[0]["status_description"] == "已完成"
    assert runs[0]["quota"] == {"max_tokens": 100, "used_tokens": 3, "over_token_quota": False}
    assert runs[0]["diagnostics"]["status"] == "completed"
    assert runs[0]["tool_calls"] == 0
    assert runs[0]["run_id"] in format_agent_runs()
    assert "summary-ok" in format_agent_run(runs[0]["run_id"])
    assert clear_agent_runs() == 1
    assert list_agent_runs() == []


@pytest.mark.asyncio
async def test_delegate_persists_agent_runs(tmp_path):
    from personal_agent.models.messages import NormalizedResponse
    from personal_agent.plugins.builtin.tools.builtin.delegate import (
        _delegate_task,
        format_agent_runs,
        load_agent_runs,
        reset_delegate,
        setup_delegate,
    )

    async def call_fn(messages, system_prompt, tools, max_tokens):
        return NormalizedResponse(text="persisted", usage={"input_tokens": 1, "output_tokens": 2})

    path = tmp_path / "runs.jsonl"
    setup_delegate(call_fn, tools=[], max_tokens=100, run_store_path=path)
    await _delegate_task("save me", role="observer")
    load_agent_runs(path)

    assert "observer" in format_agent_runs()
    assert "save me" in format_agent_runs()
    reset_delegate()


@pytest.mark.asyncio
async def test_delegate_stop_cancels_running_agent():
    from personal_agent.models.messages import NormalizedResponse
    from personal_agent.plugins.builtin.tools.builtin.delegate import (
        _delegate_task,
        list_agent_runs,
        setup_delegate,
        stop_delegate_agents,
    )

    started = asyncio.Event()

    async def call_fn(messages, system_prompt, tools, max_tokens):
        started.set()
        await asyncio.sleep(60)
        return NormalizedResponse(text="late")

    setup_delegate(call_fn, tools=[], max_tokens=100)
    task = asyncio.create_task(_delegate_task("slow"))
    await started.wait()

    stopped = stop_delegate_agents()
    result = await task
    runs = list_agent_runs()

    assert stopped == 1
    assert "stopped" in result
    assert runs[0]["status"] == "cancelled"
    assert runs[0]["stop_requested"] is True


# ── tools are registered ────────────────────────────────


def test_all_new_tools_registered():
    from personal_agent.tools.registry import tool_registry

    expected = [
        "clarify", "execute_code",
        "sub_agent", "sub_parallel", "sub_pipeline",
        "process_start", "process_list", "process_read", "process_clear", "process_kill", "process_wait",
    ]
    for name in expected:
        entry = tool_registry.get(name)
        assert entry is not None, f"Tool '{name}' not registered"
        assert entry.toolset in ("builtin",)


def test_tool_descriptions_guide_model_usage():
    import personal_agent.plugins.builtin.tools.builtin.bash  # noqa: F401
    import personal_agent.plugins.builtin.tools.builtin.file_edit  # noqa: F401
    import personal_agent.plugins.builtin.tools.builtin.file_write  # noqa: F401
    import personal_agent.plugins.builtin.tools.builtin.process_tool  # noqa: F401
    from personal_agent.tools.registry import tool_registry

    assert "process_start" in tool_registry.get("bash").description
    assert "long-running" in tool_registry.get("process_start").description
    assert "since_last" in tool_registry.get("process_read").description
    assert "finished" in tool_registry.get("process_clear").description
    assert "first occurrence" in tool_registry.get("edit").description
    assert "Overwrite" in tool_registry.get("write").description
