"""WorkflowRegistry — register, discover, and run named workflows."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


class WorkflowRegistry:
    def __init__(self) -> None:
        self._workflows: dict[str, WorkflowDef] = {}

    def register(self, defn: WorkflowDef) -> None:
        self._workflows[defn.name] = defn
        logger.debug("Workflow registered: %s", defn.name)

    def get(self, name: str) -> WorkflowDef | None:
        return self._workflows.get(name)

    def list(self) -> list[WorkflowDef]:
        return list(self._workflows.values())

    def list_names(self) -> list[str]:
        return list(self._workflows.keys())


@dataclass
class WorkflowDef:
    name: str
    description: str
    fn: Callable  # async fn(args: Any) -> Any
    phases: list[str] = field(default_factory=list)  # phases for progress display
    when_to_use: str = ""  # hint for the LLM about when to use this workflow


# Module-level singleton
workflow_registry = WorkflowRegistry()
