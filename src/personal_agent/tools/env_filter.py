"""Credential env filtering — prevent API keys from leaking to subprocesses.

Used by bash tool and MCP client before spawning child processes.
The blocklist is absolute — not even explicit config can override it.
"""

from __future__ import annotations

import os

# Env var names that contain credentials — NEVER passed to subprocesses.
# These match by prefix, suffix, or exact name.
_BLOCKED_EXACT: set[str] = {
    "LLM_API_KEY",
    "LLM_API_SECRET",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "WEIXIN_TOKEN",
    "WEIXIN_ACCOUNT_ID",
    "WEIXIN_USER_ID",
    "QQ_BOT_TOKEN",
    "QQ_BOT_WEBHOOK_SECRET",
}

_BLOCKED_SUFFIXES: tuple[str, ...] = (
    "_API_KEY",
    "_API_SECRET",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_PASSPHRASE",
)

_BLOCKED_PREFIXES: tuple[str, ...] = (
    "ANTHROPIC_",
    "OPENAI_",
    "DEEPSEEK_",
    "GEMINI_",
    "OPENROUTER_",
)


def filter_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return a copy of env with credential-bearing vars removed.

    Pass None to use os.environ.
    """
    source = env if env is not None else os.environ
    filtered: dict[str, str] = {}

    for key, value in source.items():
        if _is_blocked(key):
            continue
        filtered[key] = value

    # Force UTF-8 to avoid GBK/cp936 garbled output on Windows
    filtered.setdefault("LANG", "en_US.UTF-8")
    filtered.setdefault("LC_ALL", "en_US.UTF-8")
    filtered["PYTHONUTF8"] = "1"
    filtered["PYTHONIOENCODING"] = "utf-8"

    return filtered


def _is_blocked(key: str) -> bool:
    upper = key.upper()
    if upper in _BLOCKED_EXACT:
        return True
    if upper.endswith(_BLOCKED_SUFFIXES):
        return True
    if upper.startswith(_BLOCKED_PREFIXES):
        return True
    return False
