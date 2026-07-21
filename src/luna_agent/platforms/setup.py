"""Host-owned helpers for interactive platform setup."""

from __future__ import annotations

import getpass
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class PlatformSetupResult:
    platform: str
    status: str = "configured"
    configured: list[str] = field(default_factory=list)
    credential_source: str = ""
    message: str = ""


@dataclass
class PlatformSetupContext:
    """Safe host capabilities exposed to a platform's setup callback."""

    root_dir: Path
    config_path: Path
    env_path: Path
    data_dir: Path
    input_fn: Callable[[str], str] = input
    secret_input_fn: Callable[[str], str] = getpass.getpass

    def env_value(self, name: str, default: str = "") -> str:
        value = os.environ.get(name)
        if value is not None:
            return str(value)
        if not self.env_path.is_file():
            return default
        for line in self.env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() != name:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            return value
        return default

    def prompt(self, label: str, *, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        value = str(self.input_fn(f"{label}{suffix}: ") or "").strip()
        return value or default

    def prompt_secret(self, label: str, *, default: str = "") -> str:
        suffix = " [已配置]" if default else ""
        value = str(self.secret_input_fn(f"{label}{suffix}: ") or "").strip()
        return value or default

    def set_env(self, name: str, value: str) -> None:
        """Update one dotenv key atomically without exposing its value."""
        if not _ENV_KEY.fullmatch(str(name)):
            raise ValueError(f"Invalid environment variable name: {name}")
        value = str(value)
        if "\n" in value or "\r" in value:
            raise ValueError(f"Environment variable contains a newline: {name}")
        self.env_path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.env_path.read_text(encoding="utf-8") if self.env_path.is_file() else ""
        rendered = _dotenv_value(value)
        lines = existing.splitlines()
        replacement = f"{name}={rendered}"
        found = False
        output: list[str] = []
        for line in lines:
            if line.lstrip().startswith(f"{name}="):
                if not found:
                    output.append(replacement)
                    found = True
                continue
            output.append(line)
        if not found:
            if output and output[-1] != "":
                output.append("")
            output.append(replacement)
        _atomic_write(self.env_path, "\n".join(output).rstrip() + "\n", mode=0o600)

    def write_credentials(self, relative_path: str, text: str) -> Path:
        path = (self.data_dir / relative_path).resolve()
        root = self.data_dir.resolve()
        if path != root and root not in path.parents:
            raise ValueError("Platform credentials path escapes data directory")
        _atomic_write(path, text, mode=0o600)
        return path


def _dotenv_value(value: str) -> str:
    if not value or re.search(r"[\s#'\"\\]", value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _atomic_write(path: Path, text: str, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)

