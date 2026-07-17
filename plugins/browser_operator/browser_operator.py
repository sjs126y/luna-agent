"""Playwright MCP registration with host-level browser restrictions."""

from __future__ import annotations

from pathlib import Path
import shutil
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from personal_agent.hooks import HookEvent, PreToolUseOutcome
from personal_agent.plugins import CommandEntry


class BrowserOperatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = "npx"
    package: str = "@playwright/mcp@0.0.78"
    browser: Literal["chromium", "chrome", "firefox", "webkit", "msedge"] = "chromium"
    executable_path: str = ""
    headless: bool = True
    isolated: bool = True
    allowed_domains: list[str] = Field(default_factory=list)
    allow_file_upload: bool = False
    allow_code_execution: bool = False
    connect_timeout_seconds: float = Field(default=120.0, gt=0)
    call_timeout_seconds: float = Field(default=120.0, gt=0)
    max_artifact_bytes: int = Field(default=10 * 1024 * 1024, gt=0)


def register(ctx) -> None:
    config = ctx.parse_config(BrowserOperatorConfig)
    work_dir = "playwright"
    args = ["-y", config.package]
    if config.browser.strip().lower() == "chromium":
        executable = _configured_browser_executable(config)
        if executable is not None:
            args.extend(["--executable-path", str(executable)])
    else:
        args.extend(["--browser", config.browser])
    if config.headless:
        args.append("--headless")
    if config.isolated:
        args.append("--isolated")
    args.extend(["--output-dir", "."])
    ctx.register_mcp_server({
        "name": "playwright",
        "transport": "stdio",
        "command": config.command,
        "args": args,
        "connect_timeout_seconds": config.connect_timeout_seconds,
        "call_timeout_seconds": config.call_timeout_seconds,
        "allow_network": True,
        "max_tools": 40,
        "max_artifact_bytes": config.max_artifact_bytes,
        "work_dir": work_dir,
        "artifact_roots": ["."],
        "artifact_extensions": [".png", ".jpg", ".jpeg", ".pdf", ".webm"],
    })
    ctx.register_skills("skills")

    allowed_domains = {_normalize_domain(value) for value in config.allowed_domains if value}

    def enforce_browser_policy(envelope):
        tool_name = str(envelope.payload.get("tool_name") or "")
        short = tool_name.rsplit("__", 1)[-1].lower()
        tool_input = dict(envelope.payload.get("tool_input") or {})
        if short in {"browser_file_upload", "file_upload"} and not config.allow_file_upload:
            return PreToolUseOutcome.block("Browser Operator file uploads are disabled")
        if short in {"browser_evaluate", "browser_run_code", "evaluate", "run_code"} and not config.allow_code_execution:
            return PreToolUseOutcome.block("Browser Operator page code execution is disabled")
        url = str(tool_input.get("url") or "").strip()
        if allowed_domains and url:
            host = _normalize_domain(urlparse(url).hostname or "")
            if not any(host == item or host.endswith(f".{item}") for item in allowed_domains):
                return PreToolUseOutcome.block(
                    f"Browser destination is outside the plugin allowlist: {host or url}"
                )
        return None

    ctx.register_hook(
        HookEvent.PRE_TOOL_USE,
        enforce_browser_policy,
        name="enforce-browser-policy",
        matcher=r"^mcp__playwright__.+$",
        priority=10,
    )
    ctx.register_command(CommandEntry(
        name="browser-status",
        description="Show Browser Operator safety configuration.",
        handler=lambda args="", **kwargs: _status(config, allowed_domains),
        scope="both",
    ))


def _normalize_domain(value: str) -> str:
    text = str(value or "").strip().lower().rstrip(".")
    if "://" in text:
        return str(urlparse(text).hostname or "").lower().rstrip(".")
    return text


def _status(config, allowed_domains: set[str]) -> str:
    domains = ", ".join(sorted(allowed_domains)) if allowed_domains else "all public domains (core network policy applies)"
    readiness = _browser_readiness(config)
    return (
        "Browser Operator\n"
        f"- browser: {config.browser or 'chromium'}\n"
        f"- readiness: {readiness}\n"
        f"- domains: {domains}\n"
        f"- file upload: {'enabled' if config.allow_file_upload else 'disabled'}\n"
        f"- page code execution: {'enabled' if config.allow_code_execution else 'disabled'}"
    )


def _browser_readiness(config: BrowserOperatorConfig) -> str:
    if shutil.which(config.command) is None:
        return f"unavailable (command not found: {config.command})"
    browser = config.browser.strip().lower()
    if browser in {"", "chromium"}:
        if _configured_browser_executable(config) is not None:
            return "ready"
        return "browser missing (run: npx playwright install chromium)"
    executable_names = {
        "chrome": ("google-chrome", "google-chrome-stable", "chrome"),
        "msedge": ("microsoft-edge", "microsoft-edge-stable"),
        "firefox": ("firefox",),
        "webkit": (),
    }
    names = executable_names.get(browser)
    if names is None:
        return f"unsupported browser: {config.browser}"
    if any(shutil.which(name) for name in names):
        return "ready"
    return f"browser missing for channel: {browser}"


def _configured_browser_executable(config: BrowserOperatorConfig) -> Path | None:
    if config.executable_path:
        candidate = Path(config.executable_path).expanduser().resolve()
        return candidate if candidate.is_file() else None
    cache_root = Path.home() / ".cache" / "ms-playwright"
    patterns = (
        "chromium-*/chrome-linux*/chrome",
        "chromium_headless_shell-*/chrome-headless-shell-linux*/headless_shell",
    )
    candidates = [path for pattern in patterns for path in cache_root.glob(pattern) if path.is_file()]
    return sorted(candidates, reverse=True)[0] if candidates else None
