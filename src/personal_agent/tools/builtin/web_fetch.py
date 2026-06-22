"""Fetch URL content and convert to Markdown."""

import httpx
from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry


async def _web_fetch(url: str) -> str:
    try:
        import html2text
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "PersonalAgent/1.0"})
            resp.raise_for_status()
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        markdown = h.handle(resp.text)
        if len(markdown) > 10000:
            markdown = markdown[:10000] + "\n\n...(truncated)"
        return markdown
    except ImportError:
        return "Error: html2text not installed"
    except Exception as e:
        return f"Error: {e}"


tool_registry.register(ToolEntry(
    name="web_fetch",
    description="Fetch a URL and convert the HTML page to Markdown text.",
    schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch"},
        },
        "required": ["url"],
    },
    handler=_web_fetch,
    toolset="builtin",
))
