"""Validated configuration for the QQ platform plugin."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class QQRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["external", "managed"] = "external"
    command: list[str] = Field(default_factory=list)
    working_dir: str = ""
    startup_timeout_seconds: float = Field(default=30, ge=1, le=600)
    stop_on_shutdown: bool = True
    restart_grace_seconds: float = Field(default=30, ge=0, le=600)

    @model_validator(mode="after")
    def validate_managed_command(self) -> "QQRuntimeConfig":
        if self.mode == "managed" and not self.command:
            raise ValueError("runtime.command is required when runtime.mode is managed")
        if any(not str(item).strip() for item in self.command):
            raise ValueError("runtime.command items must be non-empty strings")
        return self


class QQPluginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime: QQRuntimeConfig = Field(default_factory=QQRuntimeConfig)
