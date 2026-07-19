"""Tests for new tools: clarify, process, execute_code, delegate."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


# ── clarify ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clarify_question_only():
    from luna_agent.plugins.builtin.tools.builtin.clarify import _clarify
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
    from luna_agent.plugins.builtin.tools.builtin.clarify import _clarify
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
    from luna_agent.plugins.builtin.tools.builtin.clarify import _clarify
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
    from luna_agent.plugins.builtin.tools.builtin.clarify import _clarify
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
    from luna_agent.plugins.builtin.tools.builtin.clarify import _clarify

    result = await _clarify("not json")
    assert "Error" in result
    assert "invalid" in result.lower()


# ── process ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_list_empty():
    from luna_agent.plugins.builtin.tools.builtin.process_tool import _process_list

    result = await _process_list()
    assert "No background processes" in result or "running" not in result.lower()


@pytest.mark.asyncio
async def test_process_kill_nonexistent():
    from luna_agent.plugins.builtin.tools.builtin.process_tool import _process_kill

    result = await _process_kill(99999)
    assert "no process" in result.lower()


@pytest.mark.asyncio
async def test_process_wait_nonexistent():
    from luna_agent.plugins.builtin.tools.builtin.process_tool import _process_wait

    result = await _process_wait(99999)
    assert "no process" in result.lower()


@pytest.mark.asyncio
async def test_process_start_read_wait(tmp_path: Path):
    from luna_agent.plugins.builtin.tools.builtin.bash import set_work_dir
    from luna_agent.plugins.builtin.tools.builtin.process_tool import (
        _process_read,
        _process_start,
        _process_wait,
    )
    from luna_agent.tools.sandbox import init_sandbox

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
    from luna_agent.plugins.builtin.tools.builtin.bash import set_work_dir
    from luna_agent.plugins.builtin.tools.builtin.process_tool import _process_start
    from luna_agent.tools.sandbox import init_sandbox

    init_sandbox([tmp_path], [])
    set_work_dir(tmp_path)

    result = await _process_start("rm -rf /", cwd=str(tmp_path))
    assert "hard blacklist" in result.lower()


@pytest.mark.asyncio
async def test_process_start_blocks_sandbox_patterns_and_mounts_declared_cwd(tmp_path: Path):
    from luna_agent.plugins.builtin.tools.builtin.bash import set_work_dir
    from luna_agent.plugins.builtin.tools.builtin.process_tool import _process_start, _process_wait
    from luna_agent.tools.sandbox import init_sandbox

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / ".env").write_text("SECRET=1", encoding="utf-8")

    init_sandbox([workspace], ["**/.env"])
    set_work_dir(workspace)

    blocked_path = await _process_start("cat .env", cwd=str(workspace))
    declared_cwd = await _process_start("pwd", cwd=str(outside))
    declared_pid = int(declared_cwd.split("Process [", 1)[1].split("]", 1)[0])
    await _process_wait(declared_pid, timeout=5)

    assert "sandbox blocked" in blocked_path.lower() or "path blocked" in blocked_path.lower()
    assert f"cwd: {outside}" in declared_cwd


@pytest.mark.asyncio
async def test_process_start_blocks_network_when_policy_denies(tmp_path: Path):
    from luna_agent.plugins.builtin.tools.builtin.bash import set_allow_network, set_work_dir
    from luna_agent.plugins.builtin.tools.builtin.process_tool import _process_start
    from luna_agent.tools.sandbox import init_sandbox

    init_sandbox([tmp_path], [])
    set_work_dir(tmp_path)
    set_allow_network(False)

    result = await _process_start("curl https://example.com", cwd=str(tmp_path))

    assert "network" in result.lower()


@pytest.mark.asyncio
async def test_process_read_invalid_stream():
    from luna_agent.plugins.builtin.tools.builtin.process_tool import _process_read

    result = await _process_read(99999, stream="wat")
    assert "no process" in result.lower()


@pytest.mark.asyncio
async def test_process_read_since_last_and_tail_modes(tmp_path: Path):
    from luna_agent.plugins.builtin.tools.builtin.bash import set_work_dir
    from luna_agent.plugins.builtin.tools.builtin.process_tool import (
        _process_read,
        _process_start,
        _process_wait,
    )
    from luna_agent.tools.sandbox import init_sandbox

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
    from luna_agent.plugins.builtin.tools.builtin.process_tool import TrackedProcess, _process_read, _processes

    _processes[98765] = TrackedProcess(pid=98765, command="demo", proc=None)  # type: ignore[arg-type]
    try:
        result = await _process_read(98765, mode="wat")
    finally:
        _processes.pop(98765, None)
    assert "mode must be one of" in result


def test_process_read_truncation_flags():
    from luna_agent.plugins.builtin.tools.builtin.process_tool import (
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
    from luna_agent.plugins.builtin.tools.builtin.bash import set_work_dir
    from luna_agent.plugins.builtin.tools.builtin.process_tool import (
        _process_clear,
        _process_kill,
        _process_list,
        _process_start,
        _process_wait,
    )
    from luna_agent.tools.sandbox import init_sandbox

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
    from luna_agent.plugins.builtin.tools.builtin.bash import set_work_dir
    from luna_agent.plugins.builtin.tools.builtin.process_tool import (
        _process_clear,
        _process_list,
        _process_start,
        _process_wait,
    )
    from luna_agent.tools.sandbox import init_sandbox

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

    from luna_agent.plugins.builtin.tools.builtin.process_tool import _process_kill

    await _process_kill(running_pid)
    await _process_clear(pid=running_pid)


@pytest.mark.asyncio
async def test_process_lifecycle():
    """Spawn a real background process, list it, wait for it, verify."""
    from luna_agent.plugins.builtin.tools.builtin.process_tool import (
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
    from luna_agent.plugins.builtin.tools.builtin.bash import _bash, set_work_dir
    from luna_agent.tools.sandbox import init_sandbox

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
    from luna_agent.plugins.builtin.tools.builtin.bash import _bash, set_work_dir
    from luna_agent.tools.sandbox import init_sandbox

    init_sandbox([tmp_path], [])
    set_work_dir(tmp_path)

    result = await _bash('python -u -c "import time\ntime.sleep(2)"', timeout=1)

    assert "Command timed out" in result
    assert "process_start" in result
    assert "exit_code:" in result


@pytest.mark.asyncio
async def test_bash_drains_large_output_with_bounded_capture(tmp_path: Path):
    from luna_agent.plugins.builtin.tools.builtin.bash import _bash, set_work_dir
    from luna_agent.tools.sandbox import init_sandbox

    init_sandbox([tmp_path], [])
    set_work_dir(tmp_path)

    result = await _bash('python -u -c "print(\'x\' * 200000)"', timeout=5)

    assert "Command finished" in result
    assert "truncated: true" in result
    assert "more bytes" in result
    assert len(result) < 10_000


@pytest.mark.asyncio
async def test_bash_strict_sandbox_blocks_obfuscated_undeclared_host_read(tmp_path: Path):
    from luna_agent.plugins.builtin.tools.builtin.bash import _bash, set_work_dir
    from luna_agent.tools import process_sandbox
    from luna_agent.tools.sandbox import init_sandbox

    if not process_sandbox.process_sandbox_capabilities()["bwrap_available"]:
        pytest.skip("bubblewrap unavailable")
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("secret", encoding="utf-8")
    init_sandbox([workspace], [])
    set_work_dir(workspace)

    command = (
        "python3 -c \"print(__import__('pathlib').Path("
        f"{str(tmp_path)!r}, 'out' + 'side.txt').exists())\""
    )
    result = await _bash(command, timeout=5)

    assert "exit_code: 0" in result
    assert "False" in result
    assert "secret" not in result


@pytest.mark.asyncio
async def test_bash_strict_sandbox_reads_only_explicit_declared_path(tmp_path: Path):
    from luna_agent.plugins.builtin.tools.builtin.bash import _bash, set_work_dir
    from luna_agent.tools import process_sandbox
    from luna_agent.tools.sandbox import init_sandbox

    if not process_sandbox.process_sandbox_capabilities()["bwrap_available"]:
        pytest.skip("bubblewrap unavailable")
    workspace = tmp_path / "workspace"
    readable = tmp_path / "readable.txt"
    sibling = tmp_path / "sibling.txt"
    workspace.mkdir()
    readable.write_text("allowed", encoding="utf-8")
    sibling.write_text("hidden", encoding="utf-8")
    init_sandbox([workspace], [])
    set_work_dir(workspace)

    command = (
        "python3 -c \"print((__import__('pathlib').Path("
        f"{str(readable)!r}).read_text(), __import__('pathlib').Path("
        f"{str(sibling)!r}).exists()))\""
    )
    result = await _bash(command, timeout=5, read_paths=[str(readable)])

    assert "exit_code: 0" in result
    assert "allowed" in result
    assert "False" in result
    assert "hidden" not in result


@pytest.mark.asyncio
async def test_bash_strict_sandbox_masks_blocked_files_inside_cwd(tmp_path: Path):
    from luna_agent.plugins.builtin.tools.builtin.bash import _bash, set_work_dir
    from luna_agent.tools import process_sandbox
    from luna_agent.tools.sandbox import init_sandbox

    if not process_sandbox.process_sandbox_capabilities()["bwrap_available"]:
        pytest.skip("bubblewrap unavailable")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("SECRET=1", encoding="utf-8")
    init_sandbox([workspace], ["**/.env"])
    set_work_dir(workspace)

    result = await _bash(
        "python3 -c \"print((lambda p: p.read_text() if p.exists() else 'hidden')"
        "(__import__('pathlib').Path('.') / ('.' + 'env')))\"",
        timeout=5,
    )

    assert "SECRET=1" not in result
    assert "PermissionError" in result or "hidden" in result


def test_bash_blocked_mount_scan_prunes_protected_directories(tmp_path: Path):
    from luna_agent.plugins.builtin.tools.builtin.bash import _collect_blocked_mounts
    from luna_agent.tools.sandbox import init_sandbox

    workspace = tmp_path / "workspace"
    protected = workspace / ".git"
    protected.mkdir(parents=True)
    (protected / "config").write_text("secret", encoding="utf-8")
    init_sandbox([workspace], ["**/.git/**"])

    assert _collect_blocked_mounts([workspace]) == (protected.absolute(),)


# ── file edit/write reliability ────────────────────────


@pytest.mark.asyncio
async def test_file_edit_rejects_empty_append(tmp_path: Path):
    from luna_agent.plugins.builtin.tools.builtin.file_edit import _file_edit
    from luna_agent.tools.sandbox import init_sandbox

    init_sandbox([tmp_path], [])

    result = await _file_edit("append", "notes.md", content="")

    assert "cannot be empty" in result.lower()


@pytest.mark.asyncio
async def test_file_edit_replace_reports_occurrences(tmp_path: Path):
    from luna_agent.plugins.builtin.tools.builtin.file_edit import _file_edit
    from luna_agent.tools.sandbox import init_sandbox

    path = tmp_path / "notes.md"
    path.write_text("alpha beta beta", encoding="utf-8")
    init_sandbox([tmp_path], [])

    empty_old = await _file_edit("replace", str(path), old_text="", new_text="x")
    missing = await _file_edit("replace", str(path), old_text="gamma", new_text="x")
    replaced = await _file_edit("replace", str(path), old_text="beta", new_text="BETA")

    assert "old_text cannot be empty" in empty_old
    assert "occurrences=0" in missing
    assert "Replaced 1 of 2 occurrences" in replaced
    assert "replace_all" in replaced
    assert path.read_text(encoding="utf-8") == "alpha BETA beta"


@pytest.mark.asyncio
async def test_file_edit_replace_all_updates_every_occurrence(tmp_path: Path):
    from luna_agent.plugins.builtin.tools.builtin.file_edit import _file_edit
    from luna_agent.tools.sandbox import init_sandbox

    path = tmp_path / "notes.md"
    path.write_text("alpha beta beta", encoding="utf-8")
    init_sandbox([tmp_path], [])

    replaced = await _file_edit("replace_all", str(path), old_text="beta", new_text="BETA")

    assert "Replaced all 2 occurrences" in replaced
    assert path.read_text(encoding="utf-8") == "alpha BETA BETA"


@pytest.mark.asyncio
async def test_file_edit_size_limit_and_sandbox_block(tmp_path: Path, monkeypatch):
    from luna_agent.plugins.builtin.tools.builtin import file_edit
    from luna_agent.tools.sandbox import init_sandbox

    path = tmp_path / "notes.md"
    path.write_text("12345", encoding="utf-8")
    init_sandbox([tmp_path], ["**/.env"])

    monkeypatch.setattr(file_edit, "_MAX_WRITE_BYTES", 6)
    too_large = await file_edit._file_edit("append", str(path), content="789")
    blocked = await file_edit._file_edit("append", ".env", content="x")

    assert "exceed max size" in too_large.lower()
    assert "path blocked" in blocked.lower()


@pytest.mark.asyncio
async def test_file_edit_rejects_existing_file_above_edit_limit(tmp_path: Path, monkeypatch):
    from luna_agent.plugins.builtin.tools.builtin import file_edit
    from luna_agent.tools.sandbox import init_sandbox

    path = tmp_path / "large.txt"
    path.write_text("0123456789", encoding="utf-8")
    init_sandbox([tmp_path], [])
    monkeypatch.setattr(file_edit, "_MAX_WRITE_BYTES", 5)

    result = await file_edit._file_edit("replace", str(path), old_text="0", new_text="x")

    assert "existing file exceeds max editable size" in result
    assert path.read_text(encoding="utf-8") == "0123456789"


@pytest.mark.asyncio
async def test_file_write_limit_uses_utf8_bytes(tmp_path: Path, monkeypatch):
    from luna_agent.plugins.builtin.tools.builtin import file_write
    from luna_agent.tools.sandbox import init_sandbox

    init_sandbox([tmp_path], [])
    monkeypatch.setattr(file_write, "_MAX_WRITE_BYTES", 5)

    result = await file_write._file_write("unicode.txt", "你好")

    assert "6 bytes" in result
    assert not (tmp_path / "unicode.txt").exists()


@pytest.mark.asyncio
async def test_grep_literal_treats_pattern_as_plain_text(tmp_path: Path):
    from luna_agent.plugins.builtin.tools.builtin.grep_tool import _grep
    from luna_agent.tools.sandbox import init_sandbox

    path = tmp_path / "app.py"
    path.write_text("foo.bar()\nfooXbar()\n", encoding="utf-8")
    init_sandbox([tmp_path], [])

    regex_result = await _grep("foo.bar()", str(tmp_path))
    literal_result = await _grep("foo.bar()", str(tmp_path), literal=True)

    assert "fooXbar()" in regex_result
    assert "foo.bar()" in literal_result
    assert "fooXbar()" not in literal_result


@pytest.mark.asyncio
async def test_task_list_search_filters_title_and_description(tmp_path: Path, monkeypatch):
    from luna_agent.plugins.builtin.tools.builtin import task as task_tool

    monkeypatch.setattr(task_tool, "_db_path", tmp_path / "tasks.db")

    await task_tool._task("add", title="Fix provider cache", description="cache hit rate")
    await task_tool._task("add", title="Polish frontend", description="activity drawer")

    title_result = await task_tool._task("list", search="provider")
    desc_result = await task_tool._task("list", search="drawer")
    empty_result = await task_tool._task("list", search="missing")

    assert "Fix provider cache" in title_result
    assert "Polish frontend" not in title_result
    assert "Polish frontend" in desc_result
    assert "Fix provider cache" not in desc_result
    assert "No tasks match" in empty_result


@pytest.mark.asyncio
async def test_execute_code_basic():
    from luna_agent.plugins.builtin.tools.builtin.execute_code import _execute_code

    result = await _execute_code("print('hello world')")
    assert "hello world" in result


@pytest.mark.asyncio
async def test_execute_code_math():
    from luna_agent.plugins.builtin.tools.builtin.execute_code import _execute_code

    result = await _execute_code("print(2 ** 10)")
    assert "1024" in result


@pytest.mark.asyncio
async def test_execute_code_stderr():
    from luna_agent.plugins.builtin.tools.builtin.execute_code import _execute_code

    result = await _execute_code("import sys; print('ok', file=sys.stderr)")
    assert "[stderr]" in result
    assert "ok" in result


@pytest.mark.asyncio
async def test_execute_code_exception():
    from luna_agent.plugins.builtin.tools.builtin.execute_code import _execute_code

    result = await _execute_code("raise RuntimeError('boom')")
    assert "RuntimeError" in result
    assert "boom" in result


@pytest.mark.asyncio
async def test_execute_code_imports():
    from luna_agent.plugins.builtin.tools.builtin.execute_code import _execute_code

    result = await _execute_code(
        "import json, math, datetime, collections; "
        "print(json.dumps({'sqrt': math.sqrt(16), 'now': str(datetime.date.today())}))"
    )
    assert "4.0" in result


@pytest.mark.asyncio
async def test_execute_code_timeout():
    from luna_agent.plugins.builtin.tools.builtin.execute_code import _execute_code

    result = await _execute_code("import time; time.sleep(120)", timeout=5)
    assert "timed out" in result.lower()


@pytest.mark.asyncio
async def test_execute_code_sandbox_env():
    """API keys should NOT be available in the sandbox."""
    from luna_agent.plugins.builtin.tools.builtin.execute_code import _execute_code

    result = await _execute_code(
        "import os; print('LLM_API_KEY' in os.environ)"
    )
    assert "False" in result


@pytest.mark.asyncio
async def test_execute_code_isolated_cwd():
    """Sandbox should run in a temp directory, not the agent's directory."""
    from luna_agent.plugins.builtin.tools.builtin.execute_code import _execute_code

    result = await _execute_code("import os; print(os.getcwd())")
    # Should be a temp dir, not the project dir
    assert "Luna Agent" not in result
    assert "Temp" in result or "tmp" in result.lower()


@pytest.mark.asyncio
async def test_execute_code_no_output():
    from luna_agent.plugins.builtin.tools.builtin.execute_code import _execute_code

    result = await _execute_code("x = 1 + 1")
    assert "no output" in result.lower()


# ── delegate_task ───────────────────────────────────────


@pytest.mark.asyncio
async def test_sub_agent_not_initialized():
    """Without setup, sub_agent should return a clear error."""
    from luna_agent.plugins.builtin.tools.builtin.delegate import _sub_agent, reset_delegate

    reset_delegate()
    result = await _sub_agent("test prompt")
    assert "not initialized" in result.lower()


@pytest.mark.asyncio
async def test_sub_parallel_bad_json():
    from luna_agent.plugins.builtin.tools.builtin.delegate import _sub_parallel

    result = await _sub_parallel("not json")
    assert "invalid" in result.lower()


@pytest.mark.asyncio
async def test_sub_agent_uses_runtime_after_setup():
    from luna_agent.models.messages import NormalizedResponse
    from luna_agent.plugins.builtin.tools.builtin.delegate import _sub_agent, setup_delegate

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
    from luna_agent.models.messages import NormalizedResponse
    from luna_agent.plugins.builtin.tools.builtin.delegate import (
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
    from luna_agent.models.messages import NormalizedResponse
    from luna_agent.plugins.builtin.tools.builtin.delegate import _delegate_task, setup_delegate

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
    from luna_agent.models.messages import NormalizedResponse
    from luna_agent.plugins.builtin.tools.builtin.delegate import (
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
    from luna_agent.models.messages import NormalizedResponse
    from luna_agent.plugins.builtin.tools.builtin.delegate import (
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
    from luna_agent.models.messages import NormalizedResponse
    from luna_agent.plugins.builtin.tools.builtin.delegate import (
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
    from luna_agent.models.messages import NormalizedResponse
    from luna_agent.plugins.builtin.tools.builtin.delegate import (
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
    from luna_agent.tools.registry import tool_registry

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
    import luna_agent.plugins.builtin.tools.builtin.bash  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.file_edit  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.file_navigation  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.file_write  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.process_tool  # noqa: F401
    from luna_agent.tools.registry import tool_registry

    assert "process_start" in tool_registry.get("bash").description
    assert "long-running" in tool_registry.get("process_start").description
    assert "since_last" in tool_registry.get("process_read").description
    assert "finished" in tool_registry.get("process_clear").description
    assert "first occurrence" in tool_registry.get("edit").description
    assert "Overwrite" in tool_registry.get("write").description


def test_builtin_tools_declare_permission_categories():
    import luna_agent.plugins.builtin.tools.builtin.bash  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.file_edit  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.file_read  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.file_write  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.glob_tool  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.grep_tool  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.process_tool  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.web_search  # noqa: F401
    from luna_agent.tools.registry import tool_registry

    expected = {
        "read": "read",
        "list_directory": "read",
        "file_info": "read",
        "grep": "read",
        "glob": "read",
        "write": "write",
        "edit": "write",
        "bash": "bash",
        "process_start": "background",
        "process_list": "background",
        "process_read": "background",
        "process_clear": "background",
        "process_kill": "background",
        "process_wait": "background",
        "web_search": "network",
    }

    for name, category in expected.items():
        assert tool_registry.get(name).permission_category == category


def test_key_builtin_tools_declare_usage_metadata():
    import luna_agent.plugins.builtin.tools.bridge.bridge  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.bash  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.file_edit  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.file_navigation  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.file_read  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.file_write  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.glob_tool  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.grep_tool  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.process_tool  # noqa: F401
    import luna_agent.plugins.builtin.tools.builtin.web_search  # noqa: F401
    from luna_agent.tools.registry import tool_registry

    expected = {
        "read": ("low", "file"),
        "list_directory": ("low", "directory"),
        "file_info": ("low", "metadata"),
        "grep": ("low", "search"),
        "glob": ("low", "search"),
        "write": ("high", "write"),
        "edit": ("high", "edit"),
        "bash": ("high", "terminal"),
        "process_start": ("high", "background"),
        "process_read": ("medium", "process"),
        "process_kill": ("high", "process"),
        "web_search": ("medium", "network"),
        "tool_search": ("low", "tooling"),
        "tool_call": ("medium", "dispatch"),
    }

    for name, (risk, tag) in expected.items():
        entry = tool_registry.get(name)
        assert entry is not None, name
        assert entry.risk_level == risk, name
        assert tag in entry.tags, name
        assert entry.usage_hint


def test_only_everyday_process_tools_are_core_and_all_remain_discoverable():
    from luna_agent.tools.toolsets import TOOLSETS, is_core_tool

    core = {"process_start", "process_read", "process_kill", "process_wait"}
    deferred = {"process_list", "process_clear"}
    for name in core | deferred:
        assert name in TOOLSETS["interact"]
    assert all(is_core_tool(name) for name in core)
    assert all(not is_core_tool(name) for name in deferred)


def test_convenience_tools_are_deferred_behind_tool_search():
    from luna_agent.tools.toolsets import get_core_tools

    core = get_core_tools()

    assert len(core) <= 20
    assert {"read", "write", "edit", "list_directory", "file_info", "grep", "glob", "bash"} <= core
    assert {"calculator", "datetime", "random", "timer", "json", "weather"}.isdisjoint(core)
    assert {"todo", "task", "workflow_run", "worktree_create", "run_review"}.isdisjoint(core)


def test_worktree_tools_declare_permission_metadata():
    import luna_agent.plugins.builtin.tools.builtin.worktree_tool  # noqa: F401
    from luna_agent.tools.registry import tool_registry

    expected = {
        "worktree_create": ("write", "medium", False),
        "worktree_merge": ("write", "high", True),
        "worktree_cleanup": ("write", "high", True),
        "worktree_list": ("read", "low", False),
    }
    for name, (category, risk, destructive) in expected.items():
        entry = tool_registry.get(name)
        assert entry.permission_category == category
        assert entry.risk_level == risk
        assert entry.is_destructive is destructive
        assert entry.usage_hint


@pytest.mark.asyncio
async def test_worktree_cleanup_refuses_dirty_worktree_without_force(tmp_path: Path, monkeypatch):
    from luna_agent.plugins.builtin.tools.builtin import worktree_tool

    worktree_root = tmp_path / "worktrees"
    dirty_tree = worktree_root / "demo"
    dirty_tree.mkdir(parents=True)
    monkeypatch.setattr(worktree_tool, "_WORKTREE_DIR", worktree_root)

    calls = []

    async def fake_git(*args, cwd=None):
        calls.append((args, cwd))
        if args[:2] == ("status", "--porcelain"):
            return 0, " M file.txt", ""
        return 0, "", ""

    monkeypatch.setattr(worktree_tool, "_git", fake_git)

    result = await worktree_tool._worktree_cleanup("demo", force=False)

    assert "uncommitted changes" in result
    assert not any(args[:2] == ("worktree", "remove") for args, _ in calls)


@pytest.mark.asyncio
async def test_worktree_cleanup_force_removes_with_force_flag(tmp_path: Path, monkeypatch):
    from luna_agent.plugins.builtin.tools.builtin import worktree_tool

    worktree_root = tmp_path / "worktrees"
    dirty_tree = worktree_root / "demo"
    dirty_tree.mkdir(parents=True)
    monkeypatch.setattr(worktree_tool, "_WORKTREE_DIR", worktree_root)

    calls = []

    async def fake_git(*args, cwd=None):
        calls.append((args, cwd))
        return 0, "", ""

    monkeypatch.setattr(worktree_tool, "_git", fake_git)

    result = await worktree_tool._worktree_cleanup("demo", force=True)

    assert "removed" in result
    assert any(args == ("worktree", "remove", str(dirty_tree), "--force") for args, _ in calls)
    assert any(args == ("branch", "-D", "worktree/demo") for args, _ in calls)
