from __future__ import annotations

import sys
from pathlib import Path

import pytest

from luna_agent.plugins.runtime.sandbox import build_plugin_worker_launch
from luna_agent.plugins.runtime.windows_sandbox import (
    _AppContainerProfileLease,
    _configure_winapi,
    _profile_name,
)


def test_process_only_requires_explicit_opt_in(tmp_path: Path) -> None:
    launch = build_plugin_worker_launch(
        python=Path(sys.executable),
        plugin_root=tmp_path / "plugin",
        environment_root=tmp_path / "environment",
        data_root=tmp_path / "data",
        backend="process-only",
    )

    assert launch.backend == "process-only"
    assert launch.filesystem_isolated is False
    assert "not an OS security boundary" in launch.warning


def test_unknown_plugin_sandbox_backend_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported plugin sandbox backend"):
        build_plugin_worker_launch(
            python=Path(sys.executable),
            plugin_root=tmp_path,
            environment_root=tmp_path,
            data_root=tmp_path / "data",
            backend="unknown",
        )


def test_appcontainer_profile_name_is_stable_and_does_not_expose_plugin_key() -> None:
    first = _profile_name("external/demo", "runtime-one")
    assert first == _profile_name("external/demo", "runtime-one")
    assert first != _profile_name("external/demo", "runtime-two")
    assert first != _profile_name("external/other", "runtime-one")
    assert "external" not in first and "/" not in first


def test_appcontainer_profile_cleanup_is_generation_owned_and_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    removed = []
    monkeypatch.setattr(
        "luna_agent.plugins.runtime.windows_sandbox._remove_appcontainer_profile",
        lambda name, roots: removed.append((name, roots)),
    )
    lease = _AppContainerProfileLease(profile_name="profile", roots=(tmp_path,))

    lease.close()
    lease.close()

    assert removed == [("profile", (tmp_path.resolve(),))]


def test_windows_api_configuration_declares_handle_width_signatures() -> None:
    class Function:
        argtypes = None
        restype = None

    class Library:
        def __getattr__(self, name):
            value = Function()
            setattr(self, name, value)
            return value

    kernel32 = Library()
    userenv = Library()
    advapi32 = Library()

    _configure_winapi(kernel32, userenv, advapi32)

    assert kernel32.CloseHandle.argtypes
    assert kernel32.CreateProcessW.argtypes
    assert kernel32.GetExitCodeProcess.argtypes
    assert kernel32.WaitForSingleObject.argtypes
    assert userenv.DeleteAppContainerProfile.argtypes


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux sandbox only")
def test_appcontainer_is_rejected_on_linux(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="only on native Windows"):
        build_plugin_worker_launch(
            python=Path(sys.executable),
            plugin_root=tmp_path,
            environment_root=tmp_path,
            data_root=tmp_path / "data",
            backend="appcontainer",
        )
