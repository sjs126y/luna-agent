from __future__ import annotations

import sys
from pathlib import Path

import pytest

from luna_agent.plugins.runtime.sandbox import build_plugin_worker_launch


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
