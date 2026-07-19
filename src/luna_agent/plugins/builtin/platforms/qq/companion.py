"""Lifecycle management for an optional plugin-owned NapCat process."""

from __future__ import annotations

import asyncio
import atexit
import logging
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, BinaryIO

from .config import QQRuntimeConfig

logger = logging.getLogger(__name__)


class NapCatCompanion:
    def __init__(self, config: QQRuntimeConfig, *, data_dir: Path) -> None:
        self.config = config
        self.data_dir = Path(data_dir)
        self._process: asyncio.subprocess.Process | None = None
        self._log_handle: BinaryIO | None = None
        self._lock = asyncio.Lock()
        self._owned = False
        self._starts = 0
        self._last_started_monotonic = 0.0
        self._last_started_at = ""
        self._last_exit_code: int | None = None
        self._last_error = ""
        self._atexit_registered = False

    @property
    def enabled(self) -> bool:
        return self.config.mode == "managed"

    @property
    def startup_timeout_seconds(self) -> float:
        return float(self.config.startup_timeout_seconds)

    async def ensure_started(self) -> bool:
        """Start NapCat once when managed and not already owned by this runtime."""
        if not self.enabled:
            return False
        async with self._lock:
            process = self._process
            if process is not None and process.returncode is None:
                return False
            if process is not None and process.returncode is not None:
                self._last_exit_code = process.returncode
            elapsed = time.monotonic() - self._last_started_monotonic
            if self._last_started_monotonic and elapsed < self.config.restart_grace_seconds:
                logger.info(
                    "NapCat was launched %.1fs ago; waiting before another start",
                    elapsed,
                )
                return False

            command, working_dir = self._validated_launch()
            log_path = self.data_dir / "logs" / "napcat.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._close_log()
            self._log_handle = log_path.open("ab", buffering=0)
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(working_dir) if working_dir else None,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=self._log_handle,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except Exception as exc:
                self._close_log()
                self._last_error = f"{type(exc).__name__}: {exc}"
                raise RuntimeError(f"NapCat launch failed: {self._last_error}") from exc

            self._owned = True
            self._starts += 1
            self._last_started_monotonic = time.monotonic()
            self._last_started_at = _timestamp()
            self._last_exit_code = None
            self._last_error = ""
            if not self._atexit_registered:
                atexit.register(self._terminate_at_exit)
                self._atexit_registered = True
            logger.info(
                "Started managed NapCat pid=%s log=%s",
                self._process.pid,
                log_path,
            )
            return True

    async def stop(self) -> None:
        if not self._owned or not self.config.stop_on_shutdown:
            return
        async with self._lock:
            process = self._process
            if process is None:
                self._close_log()
                return
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        process.kill()
                    with suppress(Exception):
                        await asyncio.wait_for(process.wait(), timeout=5)
            self._last_exit_code = process.returncode
            self._process = None
            self._owned = False
            self._close_log()
            logger.info("Stopped managed NapCat exit_code=%s", self._last_exit_code)

    def snapshot(self) -> dict[str, Any]:
        process = self._process
        running = bool(process is not None and process.returncode is None)
        if process is not None and process.returncode is not None:
            self._last_exit_code = process.returncode
        return {
            "mode": self.config.mode,
            "managed": self.enabled,
            "owned": self._owned,
            "running": running,
            "pid": process.pid if running else None,
            "starts": self._starts,
            "restarts": max(0, self._starts - 1),
            "stop_on_shutdown": self.config.stop_on_shutdown,
            "startup_timeout_seconds": self.config.startup_timeout_seconds,
            "last_started_at": self._last_started_at,
            "last_exit_code": self._last_exit_code,
            "last_error": self._last_error,
            "log_path": str(self.data_dir / "logs" / "napcat.log"),
        }

    def _validated_launch(self) -> tuple[list[str], Path | None]:
        command = [str(item) for item in self.config.command]
        executable = Path(command[0]).expanduser()
        if not executable.is_absolute():
            raise RuntimeError("NapCat managed command executable must be an absolute path")
        if not executable.exists() or not executable.is_file():
            raise RuntimeError(f"NapCat managed executable not found: {executable}")

        working_dir = None
        if self.config.working_dir:
            working_dir = Path(self.config.working_dir).expanduser()
            if not working_dir.is_absolute() or not working_dir.is_dir():
                raise RuntimeError(f"NapCat managed working directory is invalid: {working_dir}")
        return [str(executable), *command[1:]], working_dir

    def _terminate_at_exit(self) -> None:
        if not self.config.stop_on_shutdown or not self._owned:
            return
        process = self._process
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()

    def _close_log(self) -> None:
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
