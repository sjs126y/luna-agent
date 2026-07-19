from __future__ import annotations

import json

from luna_agent.artifacts import ArtifactStoreError
from luna_agent.tools.entry import ToolEntry, ToolHandlerOutput
from luna_agent.tools.registry import tool_registry
from luna_agent.tools.runtime_context import current_tool_agent


async def response_attach(artifact_ids: list[str]) -> ToolHandlerOutput:
    agent = current_tool_agent()
    store = getattr(agent, "_artifact_store", None) if agent is not None else None
    draft = getattr(agent, "_response_draft", None) if agent is not None else None
    if store is None or draft is None:
        return ToolHandlerOutput(
            text="Response artifact selection is unavailable in this runtime.",
            is_error=True,
            metadata={"reason_code": "response_draft_unavailable"},
        )
    requested = list(dict.fromkeys(str(item or "").strip() for item in artifact_ids if str(item or "").strip()))
    if not requested:
        return ToolHandlerOutput(
            text="At least one artifact_id is required.",
            is_error=True,
            metadata={"reason_code": "artifact_ids_empty"},
        )
    try:
        selected = await draft.attach(store, requested)
    except ArtifactStoreError as exc:
        return ToolHandlerOutput(
            text=f"Unable to attach response artifact: {exc.reason}",
            is_error=True,
            metadata={"reason_code": exc.reason},
        )
    summaries = [item.safe_summary() for item in selected]
    return ToolHandlerOutput(
        text=json.dumps({"selected": summaries}, ensure_ascii=False, sort_keys=True),
        metadata={
            "selected_artifact_ids": [item.artifact_id for item in selected],
            "selected_artifact_count": len(selected),
        },
    )


tool_registry.register(ToolEntry(
    name="response_attach",
    description=(
        "Attach one or more artifacts produced during the current turn to the final response. "
        "Use this when the user asks to receive or view a generated image, audio, video, or file. "
        "This selects artifacts only; provide the normal final response text afterwards."
    ),
    schema={
        "type": "object",
        "properties": {
            "artifact_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 10,
            },
        },
        "required": ["artifact_ids"],
        "additionalProperties": False,
    },
    handler=response_attach,
    toolset="utility",
    permission_category="default",
    approval_mode="auto",
    idempotent=True,
    is_parallel_safe=False,
    risk_level="low",
    usage_hint="Only use artifact IDs returned by tools in the current turn.",
))
