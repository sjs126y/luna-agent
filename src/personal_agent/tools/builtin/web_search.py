"""Web search via DuckDuckGo."""

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry


async def _web_search(query: str, max_results: int = 5) -> str:
    try:
        from duckduckgo_search import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append(f"- [{r.get('title', '')}]({r.get('href', '')})\n  {r.get('body', '')}")
        if not results:
            return "No results found."
        return "\n\n".join(results)
    except ImportError:
        return "Error: duckduckgo-search not installed"
    except Exception as e:
        return f"Error: {e}"


tool_registry.register(ToolEntry(
    name="web_search",
    description="Search the web using DuckDuckGo. Returns titles, URLs, and snippets.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {"type": "integer", "description": "Max results (default 5)"},
        },
        "required": ["query"],
    },
    handler=_web_search,
    toolset="builtin",
))
