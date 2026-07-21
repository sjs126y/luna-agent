import base64
import os

import pytest


def _windows_capabilities(path: str = r"C:\Program Files\PowerShell\7\pwsh.exe"):
    return {
        "platform": "win32",
        "bwrap_path": "",
        "bwrap_available": False,
        "powershell_path": path,
        "powershell_available": bool(path),
        "job_object_available": True,
        "network_namespace_available": False,
    }


def test_windows_shell_launch_uses_pwsh_and_encoded_utf8_command(tmp_path, monkeypatch):
    from luna_agent.tools import process_sandbox

    monkeypatch.setattr(process_sandbox, "_is_windows", lambda: True)
    monkeypatch.setattr(
        process_sandbox,
        "process_sandbox_capabilities",
        lambda: _windows_capabilities(),
    )

    launch = process_sandbox.build_shell_process_launch(
        "Write-Output '你好'",
        cwd=tmp_path,
        writable_roots=[tmp_path],
        allow_network=False,
    )

    assert launch.backend == "windows-powershell"
    assert launch.argv[0].endswith("pwsh.exe")
    assert launch.argv[1:5] == (
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
    )
    decoded = base64.b64decode(launch.argv[5]).decode("utf-16-le")
    assert "Write-Output '你好'" in decoded
    assert launch.security_level == "controlled-host"
    assert launch.process_tree_managed is True
    assert launch.filesystem_isolated is False


def test_windows_shell_requires_powershell7(tmp_path, monkeypatch):
    from luna_agent.tools import process_sandbox

    monkeypatch.setattr(process_sandbox, "_is_windows", lambda: True)
    monkeypatch.setattr(
        process_sandbox,
        "process_sandbox_capabilities",
        lambda: _windows_capabilities(path=""),
    )

    launch = process_sandbox.build_shell_process_launch(
        "Get-Location",
        cwd=tmp_path,
        writable_roots=[tmp_path],
        allow_network=False,
    )

    assert launch.backend == "unavailable"
    assert "PowerShell 7" in launch.warning


def test_windows_snapshot_reports_controlled_host(monkeypatch):
    from luna_agent.tools import process_sandbox

    monkeypatch.setattr(process_sandbox, "_is_windows", lambda: True)
    monkeypatch.setattr(
        process_sandbox,
        "process_sandbox_capabilities",
        lambda: _windows_capabilities(),
    )

    snapshot = process_sandbox.process_sandbox_snapshot("auto")

    assert snapshot["effective_backend"] == "windows-powershell"
    assert snapshot["bash_effective_backend"] == "windows-powershell"
    assert snapshot["security_level"] == "controlled-host"
    assert snapshot["process_tree_managed"] is True
    assert snapshot["bash_fail_closed"] is False
    assert "controlled-host" in " ".join(snapshot["warnings"])


def test_windows_snapshot_fails_closed_without_powershell(monkeypatch):
    from luna_agent.tools import process_sandbox

    monkeypatch.setattr(process_sandbox, "_is_windows", lambda: True)
    monkeypatch.setattr(
        process_sandbox,
        "process_sandbox_capabilities",
        lambda: _windows_capabilities(path=""),
    )

    snapshot = process_sandbox.process_sandbox_snapshot("auto")

    assert snapshot["effective_backend"] == "unavailable"
    assert snapshot["bash_effective_backend"] == "unavailable"
    assert snapshot["bash_fail_closed"] is True
    assert any("PowerShell 7" in warning for warning in snapshot["warnings"])


def test_windows_snapshot_rejects_explicit_bwrap(monkeypatch):
    from luna_agent.tools import process_sandbox

    monkeypatch.setattr(process_sandbox, "_is_windows", lambda: True)
    monkeypatch.setattr(
        process_sandbox,
        "process_sandbox_capabilities",
        lambda: _windows_capabilities(),
    )

    snapshot = process_sandbox.process_sandbox_snapshot("bwrap")

    assert snapshot["effective_backend"] == "unavailable"
    assert snapshot["bash_effective_backend"] == "unavailable"
    assert snapshot["bash_fail_closed"] is True


def test_windows_config_template_uses_local_appdata(monkeypatch):
    import luna_agent.cli as cli

    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(cli, "default_data_dir", lambda: cli.Path(r"C:\Users\tester\AppData\Local\LunaAgent"))

    rendered = cli._config_template("local")

    assert 'data_dir: "C:/Users/tester/AppData/Local/LunaAgent"' in rendered
    assert 'bash_work_dir: "C:/Users/tester/AppData/Local/LunaAgent"' in rendered
    assert '    - "C:/Users/tester/AppData/Local/LunaAgent"' in rendered
    assert '    - "C:/Users/tester/AppData/Local/LunaAgent/plugins"' in rendered


def test_windows_job_helper_is_noop_off_windows(monkeypatch):
    from luna_agent.tools import windows_job

    monkeypatch.setattr(windows_job.os, "name", "posix")
    assert windows_job.available() is False
    assert windows_job.attach(12345) is False
    assert windows_job.terminate(12345) is False
    windows_job.release(12345)


def test_windows_whitelist_accepts_native_read_command_and_blocks_network(monkeypatch):
    from luna_agent.plugins.builtin.tools.builtin import bash

    monkeypatch.setattr(bash, "_is_windows", lambda: True)
    monkeypatch.setattr(bash, "_allow_network", False)

    assert bash._check_command("Get-Location") is None
    assert bash._check_command("Get-Content notes.txt") is None
    blocked = bash._check_command("Invoke-WebRequest https://example.com")
    assert blocked is not None
    assert "network" in blocked.lower()


def test_windows_whitelist_rejects_power_shell_escape_forms(monkeypatch):
    from luna_agent.plugins.builtin.tools.builtin import bash

    monkeypatch.setattr(bash, "_is_windows", lambda: True)

    for command in (
        "Invoke-Expression $x",
        "Start-Process powershell",
        "[System.IO.File]::ReadAllText('secret.txt')",
    ):
        assert bash._check_command(command) is not None


def test_sensitive_file_protection_uses_icacls_on_windows(tmp_path, monkeypatch):
    from luna_agent.tools import file_security

    target = tmp_path / ".env"
    target.write_text("TOKEN=secret", encoding="utf-8")
    calls = []

    class Result:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(file_security, "_is_windows", lambda: True)
    monkeypatch.setattr(file_security, "_windows_identity", lambda: "DESKTOP\\user")
    monkeypatch.setattr(
        file_security.subprocess,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs)) or Result(),
    )

    file_security.secure_file(target)

    assert calls[0][0] == [
        "icacls",
        str(target),
        "/inheritance:r",
        "/grant:r",
        "DESKTOP\\user:F",
    ]


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="native Windows runtime only")
async def test_native_windows_shell_executes_power_shell7(tmp_path):
    from luna_agent.plugins.builtin.tools.builtin import bash

    bash.set_work_dir(tmp_path)
    bash.set_process_backend("auto")
    bash.set_allow_network(False)
    bash.set_restrict_paths(True)

    result = await bash._bash("Write-Output 'windows-runtime-ok'", timeout=5)

    assert "Command finished" in result
    assert "windows-runtime-ok" in result
