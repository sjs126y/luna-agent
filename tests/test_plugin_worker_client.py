from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from luna_agent.plugins.runtime.worker_client import PluginWorkerClient


def _worker_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }


@pytest.mark.asyncio
async def test_worker_registers_and_invokes_tool_without_stdout_pollution(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    data = tmp_path / "data"
    plugin.mkdir()
    (plugin / "demo.py").write_text(
        "from luna_agent_plugin_sdk import ToolEntry\n"
        "print('plugin import noise')\n"
        "async def echo(text): return {'echo': text}\n"
        "def register(ctx):\n"
        "    print('plugin register noise')\n"
        "    ctx.register.tool(ToolEntry(name='worker_echo', description='echo', "
        "schema={'type':'object'}, handler=echo))\n",
        encoding="utf-8",
    )
    client = PluginWorkerClient(
        python=Path(sys.executable),
        cwd=plugin,
        env=_worker_env(),
    )
    result = client.start({
        "plugin_key": "external/demo",
        "generation_id": "external/demo@g1",
        "runtime_instance_id": "external-demo:r1",
        "plugin_root": str(plugin),
        "data_root": str(data),
        "entrypoint": "demo:register",
        "config": {},
    })
    try:
        descriptor = result["capabilities"]["tools"][0]
        assert descriptor["name"] == "worker_echo"
        invoked = await client.call("invoke", {
            "handler_id": descriptor["handler_id"],
            "kwargs": {"text": "ok"},
        })
        assert invoked == {"echo": "ok"}
        assert "plugin import noise" in client.last_stderr
        assert (await client.call("health", {}))["ready"] is True
    finally:
        client.stop()


def test_worker_rejects_unsupported_host_callbacks(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "bad.py").write_text(
        "from luna_agent_plugin_sdk import ToolEntry\n"
        "async def run(): return 'ok'\n"
        "def register(ctx):\n"
        "    ctx.register.tool(ToolEntry(name='bad', description='bad', schema={}, "
        "handler=run, precheck=lambda value: None))\n",
        encoding="utf-8",
    )
    client = PluginWorkerClient(
        python=Path(sys.executable),
        cwd=plugin,
        env=_worker_env(),
    )
    with pytest.raises(Exception, match="unsupported host callbacks"):
        client.start({
            "plugin_key": "external/bad",
            "generation_id": "external/bad@g1",
            "runtime_instance_id": "external-bad:r1",
            "plugin_root": str(plugin),
            "data_root": str(tmp_path / "data"),
            "entrypoint": "bad:register",
            "config": {},
        })
