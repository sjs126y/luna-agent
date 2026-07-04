"""Web search — Bing scraping (no API key) with ddgs fallback.

Priority:
  1. Bing scraping (cn.bing.com) — no key, works from China
  2. ddgs (DuckDuckGo) — fallback if Bing is unreachable
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry

logger = logging.getLogger(__name__)

# ── Bing scraper ──────────────────────────────────────

_BING_URL = "https://cn.bing.com/search"
_BING_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# Extract result blocks from Bing HTML
# Bing wraps each result in <li class="b_algo"> with:
#   <h2><a href="...">title</a></h2>
#   <p> or <div class="b_caption"> → snippet
_RESULT_RE = re.compile(
    r'<li\s+class="b_algo"[^>]*>'
    r'(.*?)'
    r'</li>',
    re.DOTALL,
)
_TITLE_RE = re.compile(r'<h2[^>]*><a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL)
_SNIPPET_RE = re.compile(
    r'<(?:p|div\s+class="b_(?:caption|algoSlug|lineclamp[^"]*))[^>]*>(.*?)</(?:p|div)>',
    re.DOTALL,
)
_TAG_RE = re.compile(r'<[^>]+>')


def _search_bing(query: str, max_results: int = 5) -> list[dict]:
    """Scrape Bing search results. Returns list of {title, url, snippet}."""
    try:
        r = httpx.get(
            _BING_URL,
            params={"q": query, "count": str(min(max_results, 10))},
            headers=_BING_HEADERS,
            timeout=15.0,
            follow_redirects=True,
        )
        r.raise_for_status()
    except Exception as exc:
        logger.debug("Bing search failed: %s", exc)
        return []

    results: list[dict] = []
    for match in _RESULT_RE.finditer(r.text):
        block = match.group(1)

        title_m = _TITLE_RE.search(block)
        if not title_m:
            continue
        url = title_m.group(1)
        title = _TAG_RE.sub("", title_m.group(2)).strip()

        snippet_m = _SNIPPET_RE.search(block)
        snippet = ""
        if snippet_m:
            snippet = _TAG_RE.sub("", snippet_m.group(1)).strip()
            snippet = re.sub(r'\s+', ' ', snippet)

        results.append({"title": title, "url": url, "snippet": snippet})

        if len(results) >= max_results:
            break

    return results


# ── ddgs fallback ─────────────────────────────────────

def _search_ddgs(query: str, max_results: int = 5) -> list[dict]:
    """DuckDuckGo search via ddgs package."""
    try:
        from ddgs import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", ""),
                })
        return results
    except ImportError:
        logger.debug("ddgs not installed")
        return []
    except Exception as exc:
        logger.debug("ddgs search failed: %s", exc)
        return []


# ── tool handler ──────────────────────────────────────

def _search_sync(query: str, max_results: int = 5) -> str:
    """Synchronous search — runs in thread pool. Try Bing first, then ddgs."""
    max_results = min(max(max_results, 1), 10)

    # Try Bing first
    results = _search_bing(query, max_results)
    backend = "Bing"

    # Fallback to ddgs
    if not results:
        results = _search_ddgs(query, max_results)
        backend = "DuckDuckGo"

    if not results:
        return "No results found."

    lines = [f"Search results ({backend}):"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. [{r['title']}]({r['url']})")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
    return "\n".join(lines)


async def _web_search(query: str, max_results: int = 5) -> str:
    try:
        return await asyncio.to_thread(_search_sync, query, max_results)
    except Exception as e:
        return f"Error: {e}"


tool_registry.register(ToolEntry(
    name="web_search",
    description="Search the web using Bing (primary) or DuckDuckGo (fallback). Returns titles, URLs, and snippets.",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {"type": "integer", "description": "Max results (default 5, max 10)"},
        },
        "required": ["query"],
    },
    handler=_web_search,
    toolset="builtin",
    permission_category="network",
    tags=["network", "web", "search"],
    risk_level="medium",
    usage_hint="Use when current or external information is needed before answering or acting.",
))
