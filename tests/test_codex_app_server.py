from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from plugins.codex_bridge.app_server import CodexAppServer, CodexAppServerError


def _server(tmp_path: Path) -> CodexAppServer:
    return CodexAppServer(
        command="codex",
        cwd=tmp_path,
        codex_home=tmp_path / "codex-home",
        approval_policy="on-request",
        approvals_reviewer="user",
        sandbox="workspace-write",
    )


@pytest.mark.asyncio
async def test_effective_config_is_forwarded_to_start_and_resume(tmp_path):
    server = _server(tmp_path)
    server._request = AsyncMock(return_value={
        "config": {
            "model": "gpt-5.6-sol",
            "model_provider": "OpenAI",
            "service_tier": "default",
        }
    })

    await server._load_effective_config()

    start = server._thread_params()
    resume = server._resume_params("thread-old")
    assert start["model"] == "gpt-5.6-sol"
    assert start["modelProvider"] == "OpenAI"
    assert start["serviceTier"] == "default"
    assert resume == {"threadId": "thread-old", **start}
    server._request.assert_awaited_once_with("config/read", {
        "includeLayers": False,
        "cwd": str(tmp_path),
    })


def test_thread_provider_mismatch_fails_closed(tmp_path):
    server = _server(tmp_path)
    server.effective_model = "gpt-5.6-sol"
    server.effective_model_provider = "OpenAI"

    with pytest.raises(CodexAppServerError, match="provider mismatch"):
        server._validate_thread_config({
            "model": "gpt-5.6-sol",
            "modelProvider": "openai",
            "thread": {"id": "thread-old"},
        })


def test_matching_thread_config_is_accepted(tmp_path):
    server = _server(tmp_path)
    server.effective_model = "gpt-5.6-sol"
    server.effective_model_provider = "OpenAI"

    server._validate_thread_config({
        "model": "gpt-5.6-sol",
        "modelProvider": "OpenAI",
        "thread": {"id": "thread-old"},
    })
