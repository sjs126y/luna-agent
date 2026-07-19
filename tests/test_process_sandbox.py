from __future__ import annotations

from pathlib import Path

import pytest


def test_bwrap_launch_is_read_only_except_configured_roots(tmp_path, monkeypatch):
    from luna_agent.tools import process_sandbox

    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(
        process_sandbox,
        "process_sandbox_capabilities",
        lambda: {
            "bwrap_available": True,
            "bwrap_path": "/usr/bin/bwrap",
            "network_namespace_available": True,
        },
    )

    launch = process_sandbox.build_process_launch(
        "touch result.txt",
        cwd=root,
        writable_roots=[root],
        allow_network=False,
        requested_backend="auto",
    )

    assert launch.backend == "bwrap"
    assert launch.filesystem_isolated is True
    assert launch.network_isolated is True
    assert launch.argv[:5] == (
        "/usr/bin/bwrap",
        "--die-with-parent",
        "--ro-bind",
        "/",
        "/",
    )
    assert ("--dev", "/dev") == tuple(
        launch.argv[launch.argv.index("--dev") : launch.argv.index("--dev") + 2]
    )
    assert ("--bind", str(root.resolve()), str(root.resolve())) == tuple(
        launch.argv[launch.argv.index("--bind") : launch.argv.index("--bind") + 3]
    )
    assert "--unshare-net" in launch.argv
    assert launch.argv[-3:] == ("/bin/sh", "-c", "touch result.txt")


def test_legacy_launch_is_explicitly_unisolated(tmp_path, monkeypatch):
    from luna_agent.tools import process_sandbox

    monkeypatch.setattr(
        process_sandbox,
        "process_sandbox_capabilities",
        lambda: {
            "bwrap_available": True,
            "bwrap_path": "/usr/bin/bwrap",
            "network_namespace_available": True,
        },
    )

    launch = process_sandbox.build_process_launch(
        "pwd",
        cwd=tmp_path,
        writable_roots=[tmp_path],
        allow_network=False,
        requested_backend="legacy",
    )

    assert launch.backend == "legacy"
    assert launch.filesystem_isolated is False
    assert launch.network_isolated is False


def test_explicit_bwrap_fails_closed_when_unavailable(tmp_path, monkeypatch):
    from luna_agent.tools import process_sandbox

    monkeypatch.setattr(
        process_sandbox,
        "process_sandbox_capabilities",
        lambda: {
            "bwrap_available": False,
            "bwrap_path": "",
            "network_namespace_available": False,
        },
    )

    launch = process_sandbox.build_process_launch(
        "pwd",
        cwd=tmp_path,
        writable_roots=[tmp_path],
        allow_network=False,
        requested_backend="bwrap",
    )

    assert launch.backend == "unavailable"
    assert launch.argv == ()
    assert "unavailable" in launch.warning


def test_process_sandbox_snapshot_reports_degraded_network(monkeypatch):
    from luna_agent.tools import process_sandbox

    monkeypatch.setattr(
        process_sandbox,
        "process_sandbox_capabilities",
        lambda: {
            "bwrap_available": True,
            "bwrap_path": "/usr/bin/bwrap",
            "network_namespace_available": False,
        },
    )

    snapshot = process_sandbox.process_sandbox_snapshot("auto")

    assert snapshot["effective_backend"] == "bwrap"
    assert snapshot["filesystem_isolated"] is True
    assert snapshot["bash_effective_backend"] == "bwrap"
    assert snapshot["bash_filesystem_isolated"] is True
    assert snapshot["bash_fail_closed"] is True
    assert snapshot["network_namespace_available"] is False
    assert snapshot["warnings"] == ["bwrap network namespace is unavailable"]


def test_process_sandbox_snapshot_reports_strict_bash_unavailable(monkeypatch):
    from luna_agent.tools import process_sandbox

    monkeypatch.setattr(
        process_sandbox,
        "process_sandbox_capabilities",
        lambda: {
            "bwrap_available": False,
            "bwrap_path": "",
            "network_namespace_available": False,
        },
    )

    snapshot = process_sandbox.process_sandbox_snapshot("auto")

    assert snapshot["effective_backend"] == "legacy"
    assert snapshot["bash_effective_backend"] == "unavailable"
    assert snapshot["bash_fail_closed"] is True
    assert "strict Bash execution is unavailable without bwrap" in snapshot["warnings"]


def test_strict_policy_fails_closed_in_auto_mode_without_bwrap(tmp_path, monkeypatch):
    from luna_agent.tools import process_sandbox

    monkeypatch.setattr(
        process_sandbox,
        "process_sandbox_capabilities",
        lambda: {
            "bwrap_available": False,
            "bwrap_path": "",
            "network_namespace_available": False,
        },
    )

    launch = process_sandbox.build_process_launch(
        "pwd",
        cwd=tmp_path,
        writable_roots=[tmp_path],
        allow_network=False,
        requested_backend="auto",
        policy=process_sandbox.BASH_STRICT_POLICY,
    )

    assert launch.backend == "unavailable"
    assert "bash-strict requires bwrap" in launch.warning


def test_strict_mount_plan_separates_runtime_and_declared_resources(tmp_path, monkeypatch):
    from luna_agent.tools import process_sandbox

    workspace = tmp_path / "workspace"
    readable = tmp_path / "readable.txt"
    writable = tmp_path / "output"
    executable = tmp_path / "custom-bin"
    workspace.mkdir()
    writable.mkdir()
    readable.write_text("readable", encoding="utf-8")
    executable.write_text("binary", encoding="utf-8")
    monkeypatch.setattr(process_sandbox, "_command_executable", lambda command: executable)

    plan = process_sandbox.build_process_mount_plan(
        policy=process_sandbox.BASH_STRICT_POLICY,
        cwd=workspace,
        command="custom-bin",
        readable_roots=[readable],
        writable_roots=[writable],
        network_isolated=True,
    )

    assert [mount.source for mount in plan.runtime_mounts] == [executable]
    assert {(mount.source, mount.access) for mount in plan.user_mounts} == {
        (readable.resolve(), "read"),
        (writable.resolve(), "write"),
        (workspace.resolve(), "write"),
    }
    assert plan.network_isolated is True


def test_strict_bwrap_hides_undeclared_host_paths(tmp_path):
    from luna_agent.tools import process_sandbox

    if not process_sandbox.process_sandbox_capabilities()["bwrap_available"]:
        pytest.skip("bubblewrap unavailable")
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("secret", encoding="utf-8")

    launch = process_sandbox.build_process_launch(
        (
            "python3 -c \"from pathlib import Path; "
            f"print(Path({str(outside)!r}).exists())\""
        ),
        cwd=workspace,
        writable_roots=[workspace],
        allow_network=False,
        policy=process_sandbox.BASH_STRICT_POLICY,
    )

    import subprocess

    completed = subprocess.run(launch.argv, capture_output=True, text=True, check=False)
    assert completed.returncode == 0
    assert completed.stdout.strip() == "False"


def test_strict_bwrap_mounts_explicit_read_path_without_its_siblings(tmp_path):
    from luna_agent.tools import process_sandbox

    if not process_sandbox.process_sandbox_capabilities()["bwrap_available"]:
        pytest.skip("bubblewrap unavailable")
    workspace = tmp_path / "workspace"
    readable = tmp_path / "readable.txt"
    sibling = tmp_path / "sibling.txt"
    workspace.mkdir()
    readable.write_text("allowed", encoding="utf-8")
    sibling.write_text("hidden", encoding="utf-8")

    launch = process_sandbox.build_process_launch(
        (
            "python3 -c \"from pathlib import Path; "
            f"print(Path({str(readable)!r}).read_text()); "
            f"print(Path({str(sibling)!r}).exists())\""
        ),
        cwd=workspace,
        readable_roots=[readable],
        writable_roots=[workspace],
        allow_network=False,
        policy=process_sandbox.BASH_STRICT_POLICY,
    )

    import subprocess

    completed = subprocess.run(launch.argv, capture_output=True, text=True, check=False)
    assert completed.returncode == 0
    assert completed.stdout.splitlines() == ["allowed", "False"]
