"""Fetch URL content and convert to Markdown."""

import asyncio
from urllib.parse import urljoin

import httpx
from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry


_MAX_REDIRECTS = 5
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_OUTPUT_CHARS = 10_000
_TEXT_CONTENT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
)


async def _checked_url(url: str) -> str | None:
    from personal_agent.tools.url_safety import check_url

    return await asyncio.to_thread(check_url, url)


async def _web_fetch(url: str) -> str:
    current_url = str(url or "").strip()
    if not current_url:
        return "Error: URL cannot be empty"

    try:
        import html2text
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            for redirect_count in range(_MAX_REDIRECTS + 1):
                url_error = await _checked_url(current_url)
                if url_error:
                    label = "redirected URL blocked" if redirect_count else "URL blocked"
                    return f"Error: {label}: {url_error}"

                async with client.stream(
                    "GET",
                    current_url,
                    headers={"User-Agent": "Lumora/1.0"},
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = str(response.headers.get("location") or "")
                        if not location:
                            return "Error: redirect response has no Location header"
                        if redirect_count >= _MAX_REDIRECTS:
                            return f"Error: too many redirects (max {_MAX_REDIRECTS})"
                        current_url = urljoin(current_url, location)
                        continue

                    response.raise_for_status()
                    content_type = str(response.headers.get("content-type") or "").lower()
                    if content_type and not content_type.startswith(_TEXT_CONTENT_TYPES):
                        return f"Error: unsupported content type: {content_type.split(';', 1)[0]}"
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > _MAX_RESPONSE_BYTES:
                            return (
                                "Error: response exceeds maximum size "
                                f"({_MAX_RESPONSE_BYTES} bytes)"
                            )
                    encoding = response.encoding or "utf-8"
                    text = bytes(content).decode(encoding, errors="replace")
                    if "html" in content_type or "<html" in text[:500].lower():
                        markdown = await asyncio.to_thread(_html_to_markdown, html2text, text)
                    else:
                        markdown = text
                    if len(markdown) > _MAX_OUTPUT_CHARS:
                        markdown = markdown[:_MAX_OUTPUT_CHARS] + "\n\n...(truncated)"
                    return markdown
        return "Error: redirect handling failed"
    except ImportError:
        return "Error: html2text not installed"
    except Exception as e:
        return f"Error: {e}"


def _html_to_markdown(html2text_module, text: str) -> str:
    converter = html2text_module.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = True
    return converter.handle(text)


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
    permission_category="network",
    tags=["network", "web", "fetch"],
    risk_level="medium",
    usage_hint="Use to read a specific URL after deciding the page is relevant.",
    timeout_seconds=35,
))
