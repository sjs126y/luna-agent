from __future__ import annotations

from pathlib import Path


def test_bwrap_launch_is_read_only_except_configured_roots(tmp_path, monkeypatch):
    from personal_agent.tools import process_sandbox

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
    assert ("--bind", str(root.resolve()), str(root.resolve())) == tuple(
        launch.argv[launch.argv.index("--bind") : launch.argv.index("--bind") + 3]
    )
    assert "--unshare-net" in launch.argv
    assert launch.argv[-3:] == ("/bin/sh", "-c", "touch result.txt")


def test_legacy_launch_is_explicitly_unisolated(tmp_path, monkeypatch):
    from personal_agent.tools import process_sandbox

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
    from personal_agent.tools import process_sandbox

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
    from personal_agent.tools import process_sandbox

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
    assert snapshot["network_namespace_available"] is False
    assert snapshot["warnings"] == ["bwrap network namespace is unavailable"]
