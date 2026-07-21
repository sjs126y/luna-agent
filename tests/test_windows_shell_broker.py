from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from luna_agent.tools.windows_shell_broker import (
    _powershell_command,
    _validate_request,
)


def _request(tmp_path: Path, **overrides):
    payload = {
        "schema_version": 1,
        "command": "Get-Location",
        "cwd": str(tmp_path),
        "sandbox_roots": [str(tmp_path)],
        "read_roots": [],
        "write_roots": [],
        "masked_paths": [],
        "allow_network": False,
        "environment": {"PATH": r"C:\\Windows\\System32", "LLM_API_KEY": "secret"},
        "profile_name": "LunaAgent.Shell.12345678",
        "acl_roots": [str(tmp_path)],
    }
    payload.update(overrides)
    return payload


def test_broker_validates_and_filters_environment(tmp_path: Path):
    result = _validate_request(_request(tmp_path))

    assert result.cwd == tmp_path.resolve()
    assert result.write_roots == (tmp_path.resolve(),)
    assert "LLM_API_KEY" not in result.environment
    assert result.environment["PYTHONUTF8"] == "1"


def test_broker_rechecks_command_policy(tmp_path: Path):
    with pytest.raises(ValueError, match="network access not allowed"):
        _validate_request(
            _request(tmp_path, command="Invoke-WebRequest https://example.com")
        )


def test_broker_rejects_invalid_profile_and_schema(tmp_path: Path):
    with pytest.raises(ValueError, match="profile"):
        _validate_request(_request(tmp_path, profile_name="bad"))
    with pytest.raises(ValueError, match="schema"):
        _validate_request(_request(tmp_path, schema_version=2))


def test_powershell_command_uses_utf16_encoded_script(tmp_path: Path):
    argv = _powershell_command(Path(r"C:\\Program Files\\PowerShell\\7\\pwsh.exe"), "Write-Output '你好'")
    assert argv[1:5] == ("-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand")
    decoded = base64.b64decode(argv[5]).decode("utf-16-le")
    assert "Write-Output '你好'" in decoded


@pytest.mark.asyncio
async def test_spawn_process_sends_broker_request_over_stdin(tmp_path: Path, monkeypatch):
    from luna_agent.tools import process_sandbox

    class Writer:
        def __init__(self):
            self.data = b""
            self.closed = False

        def write(self, data):
            self.data += data

        async def drain(self):
            return None

        def close(self):
            self.closed = True

    class Process:
        def __init__(self):
            self.stdin = Writer()
            self.pid = 123
            self.returncode = None

        def kill(self):
            self.returncode = -9

        async def wait(self):
            self.returncode = 0
            return 0

    process = Process()
    calls = []

    async def create(*argv, **kwargs):
        calls.append((argv, kwargs))
        return process

    monkeypatch.setattr(process_sandbox.asyncio, "create_subprocess_exec", create)
    request = _request(tmp_path)
    launch = process_sandbox.ProcessLaunchSpec(
        backend="windows-appcontainer",
        argv=("python.exe", "-m", "luna_agent.tools.windows_shell_broker"),
        cwd=tmp_path,
        filesystem_isolated=True,
        network_isolated=True,
        broker_request=request,
    )

    result = await process_sandbox.spawn_process(
        launch,
        command="Get-Location",
        environment={"PATH": "safe"},
        stdout=-1,
        stderr=-1,
        blocked_patterns=("**/.env",),
    )

    payload = json.loads(process.stdin.data.decode("utf-8"))
    assert result is process
    assert payload["command"] == "Get-Location"
    assert payload["environment"] == {"PATH": "safe"}
    assert payload["blocked_patterns"] == ["**/.env"]
    assert process.stdin.closed is True
    assert calls[0][0][-1] == "luna_agent.tools.windows_shell_broker"
