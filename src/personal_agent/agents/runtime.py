"""Controlled sub-agent runtime."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

READONLY_TOOLS = {
    "read",
    "grep",
    "glob",
    "web_search",
    "web_fetch",
    "calculator",
    "datetime",
    "weather",
    "random",
    "json",
    "todo",
    "task",
    "process_list",
}

DESTRUCTIVE_TOOLS = {
    "write",
    "edit",
    "bash",
    "execute_code",
    "process_kill",
    "memory",
    "memory_ingest",
}

NEVER_DELEGATE = {
    "sub_agent",
    "sub_parallel",
    "sub_pipeline",
    "delegate_task",
    "run_review",
    "run_research",
    "run_workflow",
    "workflow_run",
    "clarify",
    "confirm",
}


@dataclass
class AgentSpec:
    role: str
    system_prompt: str = ""
    model: str = ""
    tool_policy: str | list[str] = "readonly"
    memory_policy: str = "none"
    timeout: float = 180.0
    max_tokens: int = 2048
    output_schema: dict | None = None


@dataclass
class AgentRun:
    run_id: str
    parent_turn_id: str
    status: str
    role: str = ""
    task: str = ""
    tool_policy: str | list[str] = "readonly"
    model: str = ""
    messages: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    duration: float = 0.0
    result: str = ""


class AgentRuntime:
    """Run one explicitly delegated, bounded sub-agent task."""

    def __init__(
        self,
        *,
        call_fn: Callable | None = None,
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
        history_limit: int = 100,
        run_store_path: Path | None = None,
    ) -> None:
        self.call_fn = call_fn
        self.tools = tools or []
        self.max_tokens = max_tokens
        self._runs: deque[AgentRun] = deque(maxlen=max(1, history_limit))
        self._run_store_path = Path(run_store_path) if run_store_path else None
        self._load_runs()

    def set_run_store(self, path: Path | None) -> None:
        self._run_store_path = Path(path) if path else None
        self._runs.clear()
        self._load_runs()

    async def run(
        self,
        task: str,
        spec: AgentSpec,
        *,
        parent_turn_id: str = "",
        allow_destructive: bool = False,
    ) -> AgentRun:
        run = AgentRun(
            run_id=uuid.uuid4().hex[:12],
            parent_turn_id=parent_turn_id,
            status="running",
            role=spec.role,
            task=task,
            tool_policy=spec.tool_policy,
            model=spec.model,
        )
        started = time.monotonic()

        if self.call_fn is None:
            run.status = "error"
            run.result = "Error: agent runtime is not initialized"
            self._record_run(run)
            return run

        tools = self._select_tools(spec.tool_policy, allow_destructive=allow_destructive)
        system_prompt = spec.system_prompt or f"You are a focused {spec.role}. Complete the delegated task."
        messages = [{"role": "user", "content": [{"type": "text", "text": task}]}]
        run.messages = messages

        try:
            response = await asyncio.wait_for(
                self.call_fn(
                    messages=messages,
                    system_prompt=system_prompt,
                    tools=tools,
                    max_tokens=min(spec.max_tokens, self.max_tokens),
                ),
                timeout=spec.timeout,
            )
            self._accumulate_usage(run.usage, response.usage)
            if response.tool_calls:
                run.tool_calls.extend(response.tool_calls)
                await self._execute_tools(response, messages, allow_destructive=allow_destructive)
                response = await asyncio.wait_for(
                    self.call_fn(
                        messages=messages + [{
                            "role": "user",
                            "content": [{"type": "text", "text": "Tools are complete. Return the final answer."}],
                        }],
                        system_prompt=system_prompt,
                        tools=[],
                        max_tokens=min(spec.max_tokens, self.max_tokens),
                    ),
                    timeout=min(spec.timeout, 120.0),
                )
                self._accumulate_usage(run.usage, response.usage)
            text = (response.text or "").strip()
            if spec.output_schema:
                text = await self._coerce_schema(
                    text,
                    spec.output_schema,
                    messages,
                    system_prompt,
                    min(spec.max_tokens, self.max_tokens),
                    spec.timeout,
                    run,
                )
            run.status = "completed"
            run.result = text
        except asyncio.TimeoutError:
            run.status = "timeout"
            run.result = "Error: delegated agent timed out"
        except Exception as exc:
            run.status = "error"
            run.result = f"Error: delegated agent failed: {exc}"
        finally:
            run.duration = time.monotonic() - started
            run.messages = messages
            self._record_run(run)
        return run

    def list_runs(self, *, limit: int | None = None) -> list[AgentRun]:
        runs = list(self._runs)
        if limit is not None:
            return runs[-max(0, limit):]
        return runs

    def get_run(self, run_id: str) -> AgentRun | None:
        for run in reversed(self._runs):
            if run.run_id == run_id:
                return run
        return None

    def clear_runs(self) -> None:
        self._runs.clear()
        if self._run_store_path and self._run_store_path.exists():
            self._run_store_path.unlink()

    def _record_run(self, run: AgentRun) -> None:
        self._runs.append(run)
        self._append_run(run)

    def _load_runs(self) -> None:
        if self._run_store_path is None or not self._run_store_path.exists():
            return
        try:
            for line in self._run_store_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                data = json.loads(line)
                self._runs.append(AgentRun(**data))
        except Exception:
            self._runs.clear()

    def _append_run(self, run: AgentRun) -> None:
        if self._run_store_path is None:
            return
        self._run_store_path.parent.mkdir(parents=True, exist_ok=True)
        with self._run_store_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(run), ensure_ascii=False) + "\n")

    def _select_tools(self, policy: str | list[str], *, allow_destructive: bool) -> list[dict]:
        if policy == "none":
            allowed: set[str] = set()
        elif policy == "all":
            allowed = {tool.get("name", "") for tool in self.tools}
        elif policy == "readonly":
            allowed = set(READONLY_TOOLS)
        elif isinstance(policy, list):
            allowed = set(policy)
        else:
            allowed = set(READONLY_TOOLS)

        if not allow_destructive:
            allowed -= DESTRUCTIVE_TOOLS
        allowed -= NEVER_DELEGATE
        return [tool for tool in self.tools if tool.get("name") in allowed]

    async def _coerce_schema(
        self,
        text: str,
        schema: dict,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int,
        timeout: float,
        run: AgentRun,
    ) -> str:
        result = _extract_json(text, schema)
        if result is not None:
            return json.dumps(result, indent=2, ensure_ascii=False)

        messages.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
        messages.append({
            "role": "user",
            "content": [{"type": "text", "text": "Return ONLY valid JSON matching the schema."}],
        })
        if self.call_fn is None:
            return f"Error: could not produce valid JSON. Raw: {text[:500]}"
        try:
            retry = await asyncio.wait_for(
                self.call_fn(
                    messages=messages,
                    system_prompt=system_prompt,
                    tools=[],
                    max_tokens=max_tokens,
                ),
                timeout=min(timeout, 60.0),
            )
            self._accumulate_usage(run.usage, retry.usage)
            retry_text = (retry.text or "").strip()
            result = _extract_json(retry_text, schema)
            if result is not None:
                return json.dumps(result, indent=2, ensure_ascii=False)
        except Exception:
            pass
        return f"Error: could not produce valid JSON. Raw: {text[:500]}"

    async def _execute_tools(self, response, messages: list[dict], *, allow_destructive: bool) -> None:
        from personal_agent.tools.executor import execute_tool_calls

        blocks = []
        if response.text:
            blocks.append({"type": "text", "text": response.text})
        for tool_call in response.tool_calls:
            blocks.append({
                "type": "tool_use",
                "id": tool_call["id"],
                "name": tool_call["name"],
                "input": tool_call["input"],
            })
        messages.append({"role": "assistant", "content": blocks})

        class _SubAgentCtx:
            _destructive_allowed: set[str] = {"all"} if allow_destructive else set()
            _tool_calls_this_turn: int = 0
            _max_tool_calls_per_turn: int = 10
            _destructive_calls_this_turn: int = 0
            _max_destructive_per_turn: int = 3

        await execute_tool_calls(response.tool_calls, messages, agent=_SubAgentCtx())

    def _accumulate_usage(self, target: dict[str, int], usage: dict[str, int]) -> None:
        for key in ("input_tokens", "output_tokens"):
            target[key] = target.get(key, 0) + int(usage.get(key, 0) or 0)


def _extract_json(text: str, schema: dict) -> dict | None:
    try:
        obj = json.loads(text)
        if _validate_schema(obj, schema):
            return obj
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            if _validate_schema(obj, schema):
                return obj
        except json.JSONDecodeError:
            pass
    return None


def _validate_schema(obj: Any, schema: dict) -> bool:
    if schema.get("type") != "object":
        return True
    if not isinstance(obj, dict):
        return False
    return all(key in obj for key in schema.get("required", []))
