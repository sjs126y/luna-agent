"""Workflow primitives — the building blocks for workflow scripts.

These are the functions available inside a workflow definition:
  agent(prompt, opts)   — spawn a single sub-agent
  parallel(thunks)      — run thunks concurrently (barrier)
  pipeline(items, *stages) — items through stages independently (no barrier)
  phase(title)          — set current progress phase
  log(message)          — emit a progress message

Each sub-agent is a single-turn LLM call (no multi-turn loop).
Schema validation is optional — pass {schema: {...}} to get structured output.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Set by engine at workflow start
_call_fn: Callable | None = None
_tools: list[dict] | None = None
_max_tokens: int = 4096
_phases: list[str] = []
_logs: list[str] = []
_current_phase: str = "default"


def _reset_context(
    call_fn: Callable,
    tools: list[dict],
    max_tokens: int = 4096,
) -> None:
    """Reset the shared context for a new workflow run."""
    global _call_fn, _tools, _max_tokens, _phases, _logs, _current_phase
    _call_fn = call_fn
    _tools = tools
    _max_tokens = max_tokens
    _phases.clear()
    _logs.clear()
    _current_phase = "default"


# ── public API (exposed to workflow scripts) ──────────


def phase(title: str) -> None:
    """Start a new phase — subsequent agent() calls are grouped under this label."""
    global _current_phase, _phases
    _current_phase = title
    if title not in _phases:
        _phases.append(title)
    log(f"[phase] {title}")


def log(message: str) -> None:
    """Emit a progress message visible to the user."""
    global _logs
    _logs.append(message)
    logger.info("Workflow: %s", message)


async def agent(
    prompt: str,
    *,
    schema: dict | None = None,
    system_prompt: str = "",
    tools: list[dict] | None = None,
    model: str = "",
    max_tokens: int | None = None,
) -> Any:
    """Spawn a single sub-agent. Returns its text or structured output.

    With schema: the sub-agent is forced to call a StructuredOutput tool,
    and the validated object is returned directly.
    Without schema: returns the agent's text response as a string.

    The sub-agent is single-turn — no conversation loop, no tool execution loop.
    """
    if _call_fn is None:
        raise RuntimeError("Workflow engine not initialized")

    # Build messages
    messages: list[dict] = []
    if system_prompt:
        messages.append({
            "role": "system",
            "content": [{"type": "text", "text": system_prompt}],
        })
    if schema:
        # Add structured output instruction
        schema_instruction = (
            f"Your response MUST be a single valid JSON object matching this schema. "
            f"Return ONLY the JSON, no other text.\n\n"
            f"Schema:\n{json.dumps(schema, indent=2, ensure_ascii=False)}"
        )
        prompt = prompt + "\n\n" + schema_instruction

    messages.append({
        "role": "user",
        "content": [{"type": "text", "text": prompt}],
    })

    # Avoid retry loop logic from the main agent
    sys = system_prompt or "You are a focused sub-agent. Complete the task and return your result."

    try:
        response = await asyncio.wait_for(
            _call_fn(
                messages=messages,
                system_prompt=sys,
                tools=tools or _tools or [],
                max_tokens=max_tokens or _max_tokens,
            ),
            timeout=180.0,
        )
    except asyncio.TimeoutError:
        log(f"Agent timed out: {prompt[:60]}...")
        return None

    text = (response.text or "").strip()

    if schema:
        # Try to extract JSON from the response
        result = _extract_json(text, schema)
        if result is None:
            log(f"Agent returned invalid JSON for schema, retrying once...")
            # One retry
            messages.append({
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            })
            messages.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": "Your response was not valid JSON matching the schema. Return ONLY the JSON object."
                }],
            })
            try:
                response2 = await asyncio.wait_for(
                    _call_fn(
                        messages=messages,
                        system_prompt=sys,
                        tools=[],
                        max_tokens=max_tokens or _max_tokens,
                    ),
                    timeout=120.0,
                )
                result = _extract_json((response2.text or "").strip(), schema)
            except Exception:
                pass
        return result

    return text


def _extract_json(text: str, schema: dict) -> dict | None:
    """Extract and validate JSON from agent response."""
    # Try direct parse
    try:
        obj = json.loads(text)
        if _validate_schema(obj, schema):
            return obj
    except json.JSONDecodeError:
        pass
    # Try to find JSON block
    import re
    m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            if _validate_schema(obj, schema):
                return obj
        except json.JSONDecodeError:
            pass
    return None


def _validate_schema(obj: dict, schema: dict) -> bool:
    """Basic schema validation — checks type and required fields."""
    if schema.get("type") != "object":
        return True  # skip validation for non-object schemas
    required = schema.get("required", [])
    for key in required:
        if key not in obj:
            return False
    return True


async def parallel(thunks: list) -> list[Any]:
    """Run all thunks concurrently. BARRIER — waits for all to complete.

    A thunk that throws resolves to None in the result array.
    """
    async def _safe(thunk):
        try:
            return await thunk()
        except Exception as e:
            log(f"Parallel thunk failed: {e}")
            return None

    return await asyncio.gather(*[_safe(t) for t in thunks])


async def pipeline(items: list, *stages) -> list[Any]:
    """Run each item through all stages independently. NO barrier between stages.

    Item A can be in stage 3 while item B is still in stage 1.
    A stage that throws drops that item to None and skips remaining stages.
    """
    async def _process_item(item, index):
        result = item
        for stage_fn in stages:
            try:
                # Pass (prevResult, originalItem, index) so stages can label work
                result = await stage_fn(result, item, index)
            except Exception as e:
                log(f"Pipeline stage failed for item {index}: {e}")
                return None
        return result

    return await asyncio.gather(*[
        _process_item(item, i) for i, item in enumerate(items)
    ])
