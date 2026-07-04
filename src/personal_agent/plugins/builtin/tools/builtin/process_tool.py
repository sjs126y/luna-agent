"""Process manager - background process tracking."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry

logger = logging.getLogger(__name__)

ProcessStatus = Literal["running", "done", "killed"]
ReadMode = Literal["tail", "all", "since_last"]


@dataclass
class TrackedProcess:
    """A single tracked background process."""

    pid: int
    command: str
    proc: asyncio.subprocess.Process
    cwd: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    status: ProcessStatus = "running"
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_read_offset: int = 0
    stderr_read_offset: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @property
    def finished(self) -> bool:
        return self.status != "running"


# In-memory registry - lost on restart, fine for a session.
_processes: dict[int, TrackedProcess] = {}
_next_id = 0
_MAX_OUTPUT = 4000


def _register(proc: asyncio.subprocess.Process, command: str, *, cwd: str = "") -> int:
    """Register a new background process. Returns the internal process id."""
    global _next_id
    _next_id += 1
    pid = _next_id
    _processes[pid] = TrackedProcess(pid=pid, command=command, proc=proc, cwd=cwd)
    asyncio.create_task(_reader(pid, "stdout", proc.stdout))
    asyncio.create_task(_reader(pid, "stderr", proc.stderr))
    asyncio.create_task(_waiter(pid, proc))
    return pid


async def _reader(pid: int, stream_name: Literal["stdout", "stderr"], reader) -> None:
    if reader is None:
        return
    try:
        while True:
            chunk = await reader.read(1024)
            if not chunk:
                return
            text = chunk.decode("utf-8", errors="replace")
            tp = _processes.get(pid)
            if tp is None:
                return
            _append_output(tp, stream_name, text)
    except Exception as exc:
        logger.debug("Process %d %s reader error: %s", pid, stream_name, exc)


async def _waiter(pid: int, proc: asyncio.subprocess.Process) -> None:
    """Wait for process completion and record final status."""
    try:
        returncode = await proc.wait()
        tp = _processes.get(pid)
        if tp:
            tp.returncode = returncode
            tp.finished_at = time.time()
            if tp.status == "running":
                tp.status = "done"
    except Exception as exc:
        logger.debug("Process %d waiter error: %s", pid, exc)


# ── tool handlers ──────────────────────────────────────


async def _process_start(command: str, cwd: str | None = None) -> str:
    """Start a background shell command and return its process id."""
    from personal_agent.plugins.builtin.tools.builtin import bash as bash_tool
    from personal_agent.tools.env_filter import filter_env

    error = bash_tool._check_command(command)
    if error:
        return error

    work_dir, cwd_error = _resolve_cwd(cwd)
    if cwd_error:
        return cwd_error

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(work_dir),
            env=filter_env(),
            **bash_tool._subprocess_group_kwargs(),
        )
        pid = _register(proc, command, cwd=str(work_dir))
        return _format_started(_processes[pid])
    except Exception as exc:
        return f"Error: {exc}"


def _process_start_precheck(input_: dict) -> str | None:
    """Run process_start hard safety checks before permission prompts."""
    from personal_agent.plugins.builtin.tools.builtin import bash as bash_tool

    command = input_.get("command", "")
    if command:
        error = bash_tool._check_command(command)
        if error:
            return error
    _, cwd_error = _resolve_cwd(input_.get("cwd"))
    return cwd_error


async def _process_list(status: str = "all", limit: int | None = None) -> str:
    """List all tracked background processes."""
    if status not in {"running", "done", "killed", "all"}:
        return "Error: status must be one of running, done, killed, all"

    processes = [
        process
        for process in sorted(_processes.values(), key=lambda item: item.pid, reverse=True)
        if status == "all" or process.status == status
    ]
    if limit is not None:
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            return "Error: limit must be an integer"
        processes = processes[:limit]

    if not processes:
        return "No background processes running."

    lines = ["Background processes:"]
    for process in processes:
        runtime = _runtime_seconds(process)
        rc = "-" if process.returncode is None else str(process.returncode)
        lines.append(
            f"  [{process.pid}] {process.status} ({runtime:.1f}s) rc={rc} - {process.command[:100]}"
        )
    return "\n".join(lines)


async def _process_read(
    pid: int,
    stream: str = "both",
    tail_chars: int = _MAX_OUTPUT,
    mode: str = "tail",
) -> str:
    """Read captured output from a background process."""
    tp = _processes.get(pid)
    if tp is None:
        return f"Error: no process with ID {pid}"
    if stream not in {"stdout", "stderr", "both"}:
        return "Error: stream must be one of stdout, stderr, both"
    if mode not in {"tail", "all", "since_last"}:
        return "Error: mode must be one of tail, all, since_last"

    tail_chars = max(1, min(int(tail_chars or _MAX_OUTPUT), _MAX_OUTPUT))
    return _format_output(
        tp,
        stream=stream,
        tail_chars=tail_chars,
        mode=mode,
        header="output",
    )


async def _process_clear(pid: int | None = None, status: str = "finished") -> str:
    """Clear finished background process records."""
    if pid is not None:
        tp = _processes.get(pid)
        if tp is None:
            return f"Error: no process with ID {pid}"
        if not tp.finished:
            return f"Error: process [{pid}] is still running. Kill or wait for it before clearing."
        del _processes[pid]
        return f"Cleared process [{pid}]."

    if status not in {"done", "killed", "finished", "all"}:
        return "Error: status must be one of done, killed, finished, all"

    def should_clear(process: TrackedProcess) -> bool:
        if not process.finished:
            return False
        if status == "finished":
            return process.status in {"done", "killed"}
        if status == "all":
            return True
        return process.status == status

    pids = [process.pid for process in _processes.values() if should_clear(process)]
    for item in pids:
        del _processes[item]
    return f"Cleared {len(pids)} process record(s)."


async def _process_kill(pid: int) -> str:
    """Kill a background process by its ID."""
    tp = _processes.get(pid)
    if tp is None:
        return f"Error: no process with ID {pid}"

    if tp.finished:
        return f"Process [{pid}] already finished (rc={tp.returncode})"

    try:
        from personal_agent.plugins.builtin.tools.builtin import bash as bash_tool

        await bash_tool._kill_process_tree(tp.proc)
        await tp.proc.wait()
        tp.status = "killed"
        tp.returncode = tp.proc.returncode if tp.proc.returncode is not None else -9
        tp.finished_at = time.time()
        await asyncio.sleep(0)
        return _format_result(tp, "killed")
    except Exception as e:
        return f"Error killing process [{pid}]: {e}"


async def _process_wait(pid: int, timeout: int = 30) -> str:
    """Wait for a process to finish and return its output."""
    tp = _processes.get(pid)
    if tp is None:
        return f"Error: no process with ID {pid}"

    if tp.finished:
        return _format_result(tp, "already finished")

    try:
        await asyncio.wait_for(tp.proc.wait(), timeout=min(timeout, 120))
        await asyncio.sleep(0)
        tp = _processes.get(pid)
        if tp and tp.finished:
            return _format_result(tp, "finished")
        return f"Process [{pid}] ended but output was not captured."
    except asyncio.TimeoutError:
        return f"Process [{pid}] still running after {timeout}s. Use process_list to check status."
    except Exception as e:
        return f"Error waiting for process [{pid}]: {e}"


def _format_started(tp: TrackedProcess) -> str:
    return (
        f"Process [{tp.pid}] started\n"
        f"status: {tp.status}\n"
        f"cwd: {tp.cwd or '-'}\n"
        f"command: {tp.command}"
    )


def _format_result(tp: TrackedProcess, status: str) -> str:
    lines = [
        f"Process [{tp.pid}] {status}",
        f"status: {tp.status}",
        f"exit_code: {tp.returncode if tp.returncode is not None else '-'}",
        f"duration: {_runtime_seconds(tp):.1f}s",
        f"command: {tp.command}",
    ]
    output = _format_output(tp, stream="both", tail_chars=_MAX_OUTPUT, mode="all", header="")
    if output:
        lines.append(output)
    return "\n".join(lines)


def _format_output(tp: TrackedProcess, *, stream: str, tail_chars: int, mode: str, header: str) -> str:
    lines = []
    if header:
        lines.extend([
            f"Process [{tp.pid}] {header}",
            f"status: {tp.status}",
            f"exit_code: {tp.returncode if tp.returncode is not None else '-'}",
            f"duration: {_runtime_seconds(tp):.1f}s",
            f"mode: {mode}",
            f"stdout_truncated: {str(tp.stdout_truncated).lower()}",
            f"stderr_truncated: {str(tp.stderr_truncated).lower()}",
        ])
    if stream in {"stdout", "both"}:
        lines.append("stdout:")
        lines.append(_read_stream(tp, "stdout", mode=mode, tail_chars=tail_chars) or "(empty)")
    if stream in {"stderr", "both"}:
        lines.append("stderr:")
        lines.append(_read_stream(tp, "stderr", mode=mode, tail_chars=tail_chars) or "(empty)")
    return "\n".join(lines)


def _read_stream(
    tp: TrackedProcess,
    stream_name: Literal["stdout", "stderr"],
    *,
    mode: str,
    tail_chars: int,
) -> str:
    value = getattr(tp, stream_name)
    if mode == "since_last":
        offset_name = f"{stream_name}_read_offset"
        offset = int(getattr(tp, offset_name))
        text = value[offset:]
        setattr(tp, offset_name, len(value))
        return text.strip()
    if mode == "all":
        return value.strip()
    return _tail(value.strip(), tail_chars)


def _resolve_cwd(cwd: str | None) -> tuple[Path, str | None]:
    if not cwd:
        from personal_agent.plugins.builtin.tools.builtin import bash as bash_tool
        return bash_tool._work_dir, None

    from personal_agent.tools.sandbox import get_sandbox

    sandbox = get_sandbox()
    full = sandbox.resolve(cwd)
    error = sandbox.check_path(full)
    if error:
        return full, error
    if not full.exists():
        return full, f"Error: cwd does not exist: {cwd}"
    if not full.is_dir():
        return full, f"Error: cwd is not a directory: {cwd}"
    return full, None


def _runtime_seconds(tp: TrackedProcess) -> float:
    end = tp.finished_at or time.time()
    return max(0.0, end - tp.started_at)


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def _append_output(tp: TrackedProcess, stream_name: Literal["stdout", "stderr"], text: str) -> None:
    current = getattr(tp, stream_name)
    combined = current + text
    if len(combined) <= _MAX_OUTPUT:
        setattr(tp, stream_name, combined)
        return

    dropped = len(combined) - _MAX_OUTPUT
    setattr(tp, stream_name, combined[dropped:])
    setattr(tp, f"{stream_name}_truncated", True)
    offset_name = f"{stream_name}_read_offset"
    offset = int(getattr(tp, offset_name))
    setattr(tp, offset_name, max(0, offset - dropped))


# ── registration ───────────────────────────────────────


tool_registry.register(ToolEntry(
    name="process_start",
    description=(
        "Start a background shell command and return a process ID. "
        "Use for long-running tests, builds, servers, watch tasks, or data processing. "
        "Then use process_read mode=since_last to poll progress."
    ),
    schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to start in the background"},
            "cwd": {"type": "string", "description": "Optional working directory within sandbox roots"},
        },
        "required": ["command"],
    },
    handler=_process_start,
    toolset="builtin",
    permission_category="background",
    tags=["terminal", "background", "process"],
    risk_level="high",
    usage_hint="Use for long-running tests, builds, servers, watchers, or commands that need polling.",
    precheck=_process_start_precheck,
    is_parallel_safe=False,
))

tool_registry.register(ToolEntry(
    name="process_list",
    description="List tracked background processes. Filter by status and limit newest results.",
    schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["running", "done", "killed", "all"],
                "description": "Filter by process status (default all)",
            },
            "limit": {"type": "integer", "description": "Maximum newest process records to show"},
        },
        "required": [],
    },
    handler=_process_list,
    toolset="builtin",
    permission_category="background",
    tags=["terminal", "background", "process"],
    risk_level="medium",
    usage_hint="Use to inspect tracked background processes before reading, waiting, killing, or clearing them.",
))

tool_registry.register(ToolEntry(
    name="process_read",
    description=(
        "Read captured stdout/stderr from a background process. "
        "Use mode=since_last when polling progress to avoid repeated log output."
    ),
    schema={
        "type": "object",
        "properties": {
            "pid": {"type": "integer", "description": "Process ID from process_start or process_list"},
            "stream": {
                "type": "string",
                "enum": ["stdout", "stderr", "both"],
                "description": "Which stream to read (default both)",
            },
            "mode": {
                "type": "string",
                "enum": ["tail", "all", "since_last"],
                "description": "Read mode: tail, all retained output, or new output since last read",
            },
            "tail_chars": {"type": "integer", "description": "Maximum tail characters to return"},
        },
        "required": ["pid"],
    },
    handler=_process_read,
    toolset="builtin",
    permission_category="background",
    tags=["terminal", "background", "process", "read"],
    risk_level="medium",
    usage_hint="Use mode=since_last to poll new output from a background process without repeating logs.",
))

tool_registry.register(ToolEntry(
    name="process_clear",
    description=(
        "Clear finished background process records. Running processes are never cleared; "
        "kill or wait for them first."
    ),
    schema={
        "type": "object",
        "properties": {
            "pid": {"type": "integer", "description": "Optional process ID to clear"},
            "status": {
                "type": "string",
                "enum": ["done", "killed", "finished", "all"],
                "description": "Which finished records to clear when pid is omitted",
            },
        },
        "required": [],
    },
    handler=_process_clear,
    toolset="builtin",
    permission_category="background",
    tags=["terminal", "background", "process"],
    risk_level="medium",
    usage_hint="Use to remove finished process records after confirming they are done or killed.",
))

tool_registry.register(ToolEntry(
    name="process_kill",
    description="Kill a running background process by its ID (from process_list).",
    schema={
        "type": "object",
        "properties": {
            "pid": {"type": "integer", "description": "Process ID from process_list"},
        },
        "required": ["pid"],
    },
    handler=_process_kill,
    toolset="builtin",
    permission_category="background",
    tags=["terminal", "background", "process"],
    risk_level="high",
    usage_hint="Use to stop a tracked running background process by pid.",
))

tool_registry.register(ToolEntry(
    name="process_wait",
    description="Wait for a background process to finish and return its final retained output.",
    schema={
        "type": "object",
        "properties": {
            "pid": {"type": "integer", "description": "Process ID from process_list"},
            "timeout": {"type": "integer", "description": "Max seconds to wait (default 30, max 120)"},
        },
        "required": ["pid"],
    },
    handler=_process_wait,
    toolset="builtin",
    permission_category="background",
    tags=["terminal", "background", "process"],
    risk_level="medium",
    usage_hint="Use to wait briefly for a tracked background process and collect final retained output.",
))
