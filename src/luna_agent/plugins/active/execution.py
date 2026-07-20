"""Backend-specific active execution adapters."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from luna_agent.plugins.active.runtime import ActivePluginRunner
from luna_agent.plugins.runtime import RuntimeBackend


@runtime_checkable
class ActiveExecution(Protocol):
    plugin: object
    registration: object
    context: object
    control: object
    root_task: object

    def start(self): ...

    async def wait_ready(self) -> None: ...

    async def quiesce(self) -> None: ...

    async def resume(self) -> None: ...

    async def stop(self) -> None: ...


class InProcessActiveExecution(ActivePluginRunner):
    """Execute a trusted generation's active entrypoint in the host loop."""


class WorkerActiveExecution(ActivePluginRunner):
    """Drive an external generation through its Worker-backed callbacks."""


def create_active_execution(*, plugin, registration, context, scope) -> ActiveExecution:
    execution_type = (
        WorkerActiveExecution
        if plugin.runtime_backend is RuntimeBackend.WORKER
        else InProcessActiveExecution
    )
    return execution_type(
        plugin=plugin,
        registration=registration,
        context=context,
        scope=scope,
    )
