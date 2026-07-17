"""Managed NapCat companion lifecycle tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from personal_agent.plugins.builtin.platforms.qq.companion import NapCatCompanion
from personal_agent.plugins.builtin.platforms.qq.config import QQPluginConfig, QQRuntimeConfig


class FakeProcess:
    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return int(self.returncode or 0)


def test_qq_plugin_config_requires_command_for_managed_mode():
    with pytest.raises(ValidationError, match="runtime.command"):
        QQPluginConfig.model_validate({"runtime": {"mode": "managed"}})

    parsed = QQPluginConfig.model_validate({})
    assert parsed.runtime.mode == "external"
    assert parsed.runtime.command == []


def test_qq_plugin_registers_shared_managed_companion(tmp_path: Path):
    from personal_agent.plugins.builtin.platforms.qq import register

    executable = tmp_path / "NapCatWinBootMain.exe"
    executable.write_bytes(b"placeholder")
    settings = SimpleNamespace(
        agent_data_dir=tmp_path / "data",
        qq_bot_ws_url="ws://127.0.0.1:16611",
    )

    class FakeContext:
        def __init__(self):
            self.settings = settings
            self.entry = None

        def parse_config(self, model_type):
            return model_type.model_validate({
                "runtime": {
                    "mode": "managed",
                    "command": [str(executable), "10001"],
                },
            })

        def register_platform(self, entry):
            self.entry = entry

    context = FakeContext()
    register(context)

    first = context.entry.factory(settings, None)
    second = context.entry.factory(settings, None)
    assert first._companion is second._companion
    assert first._companion.enabled is True


@pytest.mark.asyncio
async def test_napcat_companion_starts_once_and_stops_owned_process(tmp_path: Path, monkeypatch):
    executable = tmp_path / "NapCatWinBootMain.exe"
    executable.write_bytes(b"placeholder")
    working_dir = tmp_path / "napcat"
    working_dir.mkdir()
    process = FakeProcess()
    calls = []

    async def fake_create_subprocess_exec(*command, **kwargs):
        calls.append((command, kwargs))
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    companion = NapCatCompanion(
        QQRuntimeConfig(
            mode="managed",
            command=[str(executable), "10001"],
            working_dir=str(working_dir),
        ),
        data_dir=tmp_path / "data",
    )

    assert await companion.ensure_started() is True
    assert await companion.ensure_started() is False
    snapshot = companion.snapshot()
    assert snapshot["running"] is True
    assert snapshot["pid"] == 4321
    assert snapshot["starts"] == 1
    assert calls[0][0] == (str(executable), "10001")
    assert calls[0][1]["cwd"] == str(working_dir)
    assert Path(snapshot["log_path"]).exists()

    await companion.stop()

    assert process.terminated is True
    assert companion.snapshot()["running"] is False
    assert companion.snapshot()["last_exit_code"] == -15


def test_napcat_companion_requires_absolute_executable():
    with pytest.raises(ValidationError, match="absolute path"):
        QQRuntimeConfig(mode="managed", command=["NapCatWinBootMain.exe"])


@pytest.mark.asyncio
async def test_napcat_companion_reports_missing_executable(tmp_path: Path):
    companion = NapCatCompanion(
        QQRuntimeConfig(mode="managed", command=[str(tmp_path / "missing.exe")]),
        data_dir=tmp_path / "data",
    )

    with pytest.raises(RuntimeError, match="not found"):
        await companion.ensure_started()


@pytest.mark.asyncio
async def test_napcat_companion_restart_grace_prevents_duplicate_launch(tmp_path: Path, monkeypatch):
    executable = tmp_path / "NapCatWinBootMain.exe"
    executable.write_bytes(b"placeholder")
    processes = [FakeProcess(1), FakeProcess(2)]
    calls = 0

    async def fake_create_subprocess_exec(*command, **kwargs):
        nonlocal calls
        process = processes[calls]
        calls += 1
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    companion = NapCatCompanion(
        QQRuntimeConfig(
            mode="managed",
            command=[str(executable)],
            restart_grace_seconds=60,
        ),
        data_dir=tmp_path / "data",
    )

    assert await companion.ensure_started() is True
    processes[0].returncode = 1
    assert await companion.ensure_started() is False
    assert calls == 1


@pytest.mark.asyncio
async def test_qq_adapter_launches_managed_companion_after_failed_probe(tmp_path: Path, monkeypatch):
    from personal_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

    class FakeCompanion:
        enabled = True
        startup_timeout_seconds = 5

        def __init__(self):
            self.starts = 0

        async def ensure_started(self):
            self.starts += 1
            return True

        async def stop(self):
            return None

        def snapshot(self):
            return {"mode": "managed", "managed": True}

    companion = FakeCompanion()
    settings = SimpleNamespace(
        qq_bot_base_url="",
        qq_bot_ws_url="ws://127.0.0.1:16611",
        qq_bot_token="",
        qq_bot_webhook_secret="",
        platform_reconnect_delays=(1,),
        platform_message_dedupe_max_size=100,
    )
    adapter = QQAdapter(settings, db=None, companion=companion)
    websocket = SimpleNamespace(closed=False)
    attempts = 0

    async def fake_open():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("connection refused")
        return websocket

    monkeypatch.setattr(adapter, "_open_websocket", fake_open)

    result = await adapter._connect_initial_websocket()

    assert result is websocket
    assert companion.starts == 1
    assert attempts == 2


@pytest.mark.asyncio
async def test_qq_adapter_reuses_ready_websocket_without_starting_companion(tmp_path: Path, monkeypatch):
    from personal_agent.plugins.builtin.platforms.qq.adapter import QQAdapter

    class FakeCompanion:
        enabled = True
        startup_timeout_seconds = 5

        def __init__(self):
            self.starts = 0

        async def ensure_started(self):
            self.starts += 1
            return True

        def snapshot(self):
            return {"mode": "managed", "managed": True}

    companion = FakeCompanion()
    settings = SimpleNamespace(
        qq_bot_base_url="",
        qq_bot_ws_url="ws://127.0.0.1:16611",
        qq_bot_token="",
        qq_bot_webhook_secret="",
        platform_reconnect_delays=(1,),
        platform_message_dedupe_max_size=100,
    )
    adapter = QQAdapter(settings, db=None, companion=companion)
    websocket = SimpleNamespace(closed=False)

    async def fake_open():
        return websocket

    monkeypatch.setattr(adapter, "_open_websocket", fake_open)

    result = await adapter._connect_initial_websocket()

    assert result is websocket
    assert companion.starts == 0
