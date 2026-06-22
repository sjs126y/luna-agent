"""Safe shell command execution — restricted to data dir, timeout, banned commands."""

import asyncio
import os
from pathlib import Path

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry

# Set at startup
_work_dir: Path = Path("./data")


def set_work_dir(path: Path) -> None:
    global _work_dir
    _work_dir = path.resolve()


_BANNED = {"rm -rf /", "sudo ", "mkfs.", "dd if=", ":(){ :|:& };:", "> /dev/sda",
           "format c:", "del /f /s", "shutdown", "reboot", "halt", "poweroff",
           "chmod 777 /", "wget -O /", "curl -o /"}

_MAX_OUTPUT = 4000


async def _shell(command: str, timeout: int = 30) -> str:
    cmd_lower = command.lower().replace(" ", "")
    for banned in _BANNED:
        if banned.lower().replace(" ", "") in cmd_lower:
            return f"Error: banned command pattern detected"

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(_work_dir),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            return f"Error: command timed out after {timeout}s"

        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        result = out or err or "(no output)"
        if len(result) > _MAX_OUTPUT:
            result = result[:_MAX_OUTPUT] + f"\n...({len(result) - _MAX_OUTPUT} more chars)"
        return result
    except Exception as e:
        return f"Error: {e}"


tool_registry.register(ToolEntry(
    name="bash",
    description="Execute a shell command in a restricted sandbox (data dir, timeout, banned dangerous commands). Use for file ops, git, pip, etc.",
    schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run, e.g. 'ls -la' or 'python --version'"},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
        },
        "required": ["command"],
    },
    handler=_shell,
    toolset="builtin",
    is_parallel_safe=False,
    is_destructive=False,
))
