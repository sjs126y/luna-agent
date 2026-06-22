"""Web search via DuckDuckGo — blocking call offloaded to thread pool."""

import asyncio

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry


def _search_sync(query: str, max_results: int = 5) -> str:
    """Synchronous search — runs in thread pool to avoid blocking event loop."""
    from duckduckgo_search import DDGS
    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append(f"- [{r.get('title', '')}]({r.get('href', '')})\n  {r.get('body', '')}")
    if not results:
        return "No results found."
    return "\n\n".join(results)


async def _web_search(query: str, max_results: int = 5) -> str:
    try:
        return await asyncio.to_thread(_search_sync, query, max_results)
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
