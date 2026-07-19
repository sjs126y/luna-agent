"""WorkflowRegistry — register, discover, and run named workflows."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


class WorkflowRegistry:
    def __init__(self) -> None:
        self._workflows: dict[str, WorkflowDef] = {}
        self._pinned: ContextVar[dict[str, WorkflowDef] | None] = ContextVar(
            f"workflow-entries:{id(self)}",
            default=None,
        )

    def register(self, defn: WorkflowDef) -> None:
        self._workflows[defn.name] = defn
        logger.debug("Workflow registered: %s", defn.name)

    def unregister(self, name: str) -> None:
        self._workflows.pop(name, None)

    def get(self, name: str) -> WorkflowDef | None:
        return self._effective().get(name)

    def list(self) -> list[WorkflowDef]:
        return list(self._effective().values())

    def list_names(self) -> list[str]:
        return list(self._effective().keys())

    @contextmanager
    def bind_entries(self, entries: dict[str, WorkflowDef]):
        token = self._pinned.set(dict(entries))
        try:
            yield
        finally:
            self._pinned.reset(token)

    def _effective(self) -> dict[str, WorkflowDef]:
        return self._pinned.get() or self._workflows


@dataclass
class WorkflowDef:
    name: str
    description: str
    fn: Callable  # async fn(args: Any) -> Any
    phases: list[str] = field(default_factory=list)  # phases for progress display
    when_to_use: str = ""  # hint for the LLM about when to use this workflow


# Module-level singleton
workflow_registry = WorkflowRegistry()
