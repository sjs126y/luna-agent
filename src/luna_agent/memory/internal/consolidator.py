"""Turn buffered observations into guarded internal-memory patches."""

from __future__ import annotations

import json

from luna_agent.memory.models import (
    InternalPatchAction,
    InternalPatchOperation,
    Observation,
)
from luna_agent.memory.prompts import INTERNAL_CONSOLIDATION_SYSTEM


class InternalMemoryConsolidator:
    def __init__(self, llm) -> None:
        self.llm = llm

    async def propose(self, *, internal_content: str, observations: list[Observation]) -> list[InternalPatchOperation]:
        prompt = (
            "Current internal memory:\n" + internal_content + "\n\nPending observations:\n" +
            json.dumps([item.as_dict() for item in observations], ensure_ascii=False) +
            "\n\nReturn {\"operations\":[{\"action\":\"ADD|UPDATE|SKIP|CONFLICT\","
            "\"observation_id\":str,\"target_file\":str,\"entry_id\":str,"
            "\"content\":str,\"reason\":str}]}"
        )
        payload = await self.llm.call_json(system_prompt=INTERNAL_CONSOLIDATION_SYSTEM, prompt=prompt)
        operations = payload.get("operations", [])
        if not isinstance(operations, list):
            raise ValueError("Internal consolidation operations must be a list")
        result: list[InternalPatchOperation] = []
        for item in operations:
            if not isinstance(item, dict):
                continue
            result.append(InternalPatchOperation(
                action=InternalPatchAction(str(item["action"]).upper()),
                observation_id=str(item["observation_id"]),
                target_file=str(item.get("target_file") or "MEMORY.md"),
                entry_id=str(item.get("entry_id") or ""),
                content=str(item.get("content") or ""),
                reason=str(item.get("reason") or ""),
            ))
        return result
