"""Memory-only prompts kept outside the main agent prompt."""

OBSERVATION_EXTRACTION_SYSTEM = """You extract durable personal-memory observations from conversations.
Return JSON only. Do not call tools and do not include markdown fences."""

OBSERVATION_EXTRACTION_PROMPT = """Extract only facts worth remembering.
Allowed kinds: preference, fact, event, relationship, commitment, behavior.
Return {{"observations": [{{"kind": str, "content": str, "importance": 0..1,
"long_term": bool, "source_turn_ids": [str]}}]}}. Return an empty list when nothing qualifies.

Conversation:
{conversation}
"""

MEMORY_RESOLUTION_SYSTEM = """You maintain a concise long-term memory store.
Compare one new observation with existing memories and return JSON only."""

INTERNAL_CONSOLIDATION_SYSTEM = """You propose structured updates to managed internal memory entries.
Never rewrite a complete file. Return JSON operations only."""
