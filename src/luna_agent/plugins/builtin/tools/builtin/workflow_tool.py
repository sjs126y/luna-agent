"""workflow_run — LLM calls named workflows to orchestrate multi-agent tasks.

Available workflows:
  - review: Multi-dimensional code review (security + performance + bugs)
            with adversarial verification.

Flow: LLM → workflow_run("review", {files: [...], dimensions: [...]})
      → engine runs workflow → returns structured result
"""

from __future__ import annotations

from luna_agent.tools.entry import ToolEntry
from luna_agent.tools.registry import tool_registry


async def _workflow_run(name: str, args: str = "{}") -> str:
    """Execute a named workflow. Returns the workflow's result as formatted text."""
    from luna_agent.workflow.engine import run_workflow_tool
    return await run_workflow_tool(name, args)


async def _workflow_list() -> str:
    """List all available workflows."""
    from luna_agent.workflow.engine import list_workflows_for_llm
    return list_workflows_for_llm()


tool_registry.register(ToolEntry(
    name="workflow_run",
    description=(
        "Execute a named multi-agent workflow. Workflows orchestrate multiple "
        "sub-agents in parallel or pipeline stages for complex tasks like code review. "
        "Use workflow_list to see available workflows. "
        "Args is a JSON object with workflow-specific parameters."
    ),
    schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Workflow name. Use workflow_list to discover available workflows.",
            },
            "args": {
                "type": "string",
                "description": "JSON object with workflow arguments, e.g. '{\"files\": [\"main.py\"]}'",
            },
        },
        "required": ["name"],
    },
    handler=_workflow_run,
    toolset="builtin",
))

tool_registry.register(ToolEntry(
    name="workflow_list",
    description="List all available workflows with descriptions and when-to-use guidance.",
    schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
    handler=_workflow_list,
    toolset="builtin",
))
