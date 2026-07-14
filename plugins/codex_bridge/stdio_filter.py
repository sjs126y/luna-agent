"""Filter experimental Codex notifications unsupported by the Python MCP SDK."""

from __future__ import annotations

import json
import subprocess
import sys
import threading


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: stdio_filter.py COMMAND [ARG ...]", file=sys.stderr)
        return 2

    process = subprocess.Popen(
        sys.argv[1:],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    input_thread = threading.Thread(target=_forward_input, args=(process,), daemon=True)
    input_thread.start()
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if _is_codex_event(line):
                continue
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
        return process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _forward_input(process: subprocess.Popen) -> None:
    assert process.stdin is not None
    try:
        for line in sys.stdin.buffer:
            process.stdin.write(line)
            process.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass


def _is_codex_event(line: bytes) -> bool:
    try:
        message = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(message, dict) and message.get("method") == "codex/event"


if __name__ == "__main__":
    raise SystemExit(main())
