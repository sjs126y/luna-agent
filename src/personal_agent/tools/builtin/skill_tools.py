"""Skill tools — LLM can discover and load skills autonomously.

skill_search: fuzzy search over registered skills
skill_load: load full skill content as tool result
"""

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry
from personal_agent.skills.registry import skill_registry


async def _skill_search(query: str) -> str:
    """Search registered skills by name/description."""
    entries = skill_registry.list()
    if not entries:
        return "No skills available."

    query_lower = query.lower()
    matches = []
    for e in entries:
        score = 0
        if query_lower in e.name.lower():
            score = 10
        elif query_lower in e.description.lower():
            score = 5
        elif any(word in e.name.lower() for word in query_lower.split()):
            score = 3
        if score > 0:
            matches.append((score, e))

    matches.sort(key=lambda x: x[0], reverse=True)

    if not matches:
        names = ", ".join(e.name for e in entries)
        return f"No matching skills found. Available: {names}"

    lines = ["Matching skills:"]
    for score, e in matches[:5]:
        lines.append(f"- {e.name}: {e.description}")
    lines.append("\nUse skill_load <name> to load a skill's full content.")
    return "\n".join(lines)


async def _skill_load(name: str) -> str:
    """Load full skill content."""
    content = skill_registry.load(name)
    if content is None:
        available = ", ".join(e.name for e in skill_registry.list())
        return f"Skill '{name}' not found. Available: {available}"
    return content


tool_registry.register(ToolEntry(
    name="skill_search",
    description="Search available skills by keyword. Returns matching skill names and descriptions. Use when you need domain expertise.",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords to search for, e.g. 'python' or 'git'"},
        },
        "required": ["query"],
    },
    handler=_skill_search,
    toolset="builtin",
    is_parallel_safe=True,
))

tool_registry.register(ToolEntry(
    name="skill_load",
    description="Load the full content of a skill by name. The skill content contains detailed instructions and best practices.",
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Exact skill name from skill_search results, e.g. 'python-expert'"},
        },
        "required": ["name"],
    },
    handler=_skill_load,
    toolset="builtin",
    is_parallel_safe=True,
))
