"""WorkflowEngine — orchestrates workflow execution.

Provides the runtime context (LLM access, tools) and invokes
workflow functions. Each workflow runs in its own context with
isolated sub-agents.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from personal_agent.workflow.registry import workflow_registry, WorkflowDef

logger = logging.getLogger(__name__)

# Global state set at boot — the engine needs LLM access
_engine_call_fn: Callable | None = None
_engine_tools: list[dict] | None = None
_engine_max_tokens: int = 4096


def setup_engine(
    call_fn: Callable,
    tools: list[dict],
    max_tokens: int = 4096,
) -> None:
    """Configure the workflow engine. Called once at agent creation."""
    global _engine_call_fn, _engine_tools, _engine_max_tokens
    _engine_call_fn = call_fn
    _engine_tools = tools
    _engine_max_tokens = max_tokens


async def run_workflow(name: str, args: Any = None) -> dict:
    """Run a named workflow. Returns {ok, result, phases, logs, elapsed_ms}."""
    if _engine_call_fn is None:
        return {"ok": False, "error": "Workflow engine not initialized"}

    wf = workflow_registry.get(name)
    if wf is None:
        available = ", ".join(workflow_registry.list_names()) or "(none)"
        return {"ok": False, "error": f"Unknown workflow: {name}. Available: {available}"}

    # ── Reset primitives context ──
    from personal_agent.workflow.primitives import _reset_context
    _reset_context(_engine_call_fn, _engine_tools or [], _engine_max_tokens)

    # ── Execute ──
    started = time.time()
    try:
        result = await asyncio.wait_for(
            wf.fn(args),
            timeout=600.0,  # 10 min max per workflow
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Workflow timed out (600s)"}
    except Exception as e:
        logger.exception("Workflow '%s' failed", name)
        return {"ok": False, "error": str(e)}

    elapsed = (time.time() - started) * 1000

    # ── Collect progress ──
    from personal_agent.workflow.primitives import _phases, _logs

    return {
        "ok": True,
        "result": result,
        "phases": list(_phases),
        "logs": list(_logs),
        "elapsed_ms": int(elapsed),
    }


async def run_workflow_tool(name: str, args: str = "{}") -> str:
    """Tool handler — LLM calls this to execute a workflow.

    Returns a formatted string with results and progress.
    """
    try:
        parsed_args = json.loads(args) if isinstance(args, str) else args
    except json.JSONDecodeError:
        parsed_args = None

    output = await run_workflow(name, parsed_args)

    if not output["ok"]:
        return f"Workflow '{name}' failed: {output.get('error', 'unknown error')}"

    lines = [f"Workflow '{name}' completed in {output['elapsed_ms']}ms"]

    if output["phases"]:
        lines.append(f"Phases: {' → '.join(output['phases'])}")

    if output["logs"]:
        lines.append("\nProgress:")
        for l in output["logs"]:
            lines.append(f"  {l}")

    lines.append(f"\nResult:")
    result = output["result"]
    if isinstance(result, (dict, list)):
        lines.append(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        lines.append(str(result))

    return "\n".join(lines)


def list_workflows_for_llm() -> str:
    """Return a summary of available workflows for the LLM prompt."""
    wfs = workflow_registry.list()
    if not wfs:
        return "No workflows available."

    lines = ["Available workflows:"]
    for w in wfs:
        lines.append(f"  {w.name}: {w.description}")
        if w.when_to_use:
            lines.append(f"    When to use: {w.when_to_use}")
    return "\n".join(lines)
