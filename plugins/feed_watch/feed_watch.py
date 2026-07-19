"""RSS and Atom subscriptions backed by the active plugin runtime."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse
from uuid import uuid4
from xml.etree import ElementTree

import httpx
from pydantic import BaseModel, ConfigDict, Field

from lumora_plugin_sdk import ActiveResourceRequest, CommandEntry, ToolEntry
from personal_agent.tools.runtime_context import current_tool_agent
from personal_agent.tools.url_safety import check_url

_MAX_FEED_BYTES = 2 * 1024 * 1024
_MAX_REDIRECTS = 5


class ActiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    sessions: list[str] = Field(default_factory=list)
    restart_backoff_seconds: list[float] = Field(default_factory=lambda: [1, 2, 5, 10, 30])


class FeedSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    url: str
    keywords: list[str] = Field(default_factory=list)
    sessions: list[str] = Field(default_factory=list)


class FeedWatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feeds: list[FeedSource] = Field(default_factory=list)
    poll_interval_seconds: float = Field(default=900.0, ge=60.0)
    max_items_per_feed: int = Field(default=30, ge=1, le=100)
    trusted_private_hosts: list[str] = Field(default_factory=list)
    active: ActiveConfig = Field(default_factory=ActiveConfig)


class FeedRepository:
    def __init__(self, storage, config: FeedWatchConfig) -> None:
        self.storage = storage
        self._lock = asyncio.Lock()
        self.state = self.storage.read_json(
            "feeds.json",
            default={"schema_version": 1, "subscriptions": {}, "feed_state": {}, "pending": []},
            schema_version=1,
        )
        changed = False
        for source in config.feeds:
            subscription = _subscription(source.name, source.url, source.keywords, source.sessions, "config")
            if subscription["subscription_id"] not in self.state["subscriptions"]:
                self.state["subscriptions"][subscription["subscription_id"]] = subscription
                changed = True
        if changed:
            self._write()

    async def subscriptions(self, *, session_key: str = "") -> list[dict[str, Any]]:
        async with self._lock:
            items = [dict(item) for item in self.state["subscriptions"].values()]
        if session_key:
            items = [item for item in items if session_key in item.get("sessions", [])]
        return sorted(items, key=lambda item: (item["name"].lower(), item["url"]))

    async def add(self, *, name: str, url: str, keywords: list[str], session_key: str) -> dict[str, Any]:
        subscription = _subscription(name, url, keywords, [session_key], "runtime")
        async with self._lock:
            existing = self.state["subscriptions"].get(subscription["subscription_id"])
            if existing is not None:
                sessions = list(dict.fromkeys([*existing.get("sessions", []), session_key]))
                existing["sessions"] = sessions
                existing["keywords"] = list(dict.fromkeys([*existing.get("keywords", []), *keywords]))
                subscription = existing
            else:
                self.state["subscriptions"][subscription["subscription_id"]] = subscription
            self._write()
        return dict(subscription)

    async def remove(self, subscription_id: str, *, session_key: str) -> bool:
        async with self._lock:
            item = self.state["subscriptions"].get(subscription_id)
            if item is None or session_key not in item.get("sessions", []):
                return False
            item["sessions"] = [value for value in item["sessions"] if value != session_key]
            if not item["sessions"] and item.get("source") != "config":
                self.state["subscriptions"].pop(subscription_id, None)
                self.state["feed_state"].pop(subscription_id, None)
            self._write()
            return True

    async def fetch_state(self, subscription_id: str) -> dict[str, Any]:
        async with self._lock:
            return dict(self.state["feed_state"].get(subscription_id) or {})

    async def update_fetch_state(self, subscription_id: str, value: dict[str, Any]) -> None:
        async with self._lock:
            self.state["feed_state"][subscription_id] = dict(value)
            self._write()

    async def pending(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [dict(item) for item in self.state["pending"]]

    async def append_pending(self, entries: list[dict[str, Any]]) -> None:
        async with self._lock:
            known = {item["event_id"] for item in self.state["pending"]}
            self.state["pending"].extend(item for item in entries if item["event_id"] not in known)
            self._write()

    async def clear_pending(self, event_ids: set[str]) -> None:
        async with self._lock:
            self.state["pending"] = [item for item in self.state["pending"] if item["event_id"] not in event_ids]
            self._write()

    def _write(self) -> None:
        self.storage.write_json_atomic("feeds.json", self.state)


class FeedWatcher:
    def __init__(self, ctx, config: FeedWatchConfig, repository: FeedRepository) -> None:
        self.ctx = ctx
        self.config = config
        self.repository = repository

    async def run(self) -> None:
        await self.ctx.runtime.ready()
        while not self.ctx.runtime.stop_requested:
            await self.ctx.runtime.wait_until_resumed()
            self.ctx.runtime.heartbeat()
            await self.poll_once()
            try:
                await asyncio.wait_for(
                    self.ctx.runtime.wait_until_stopped(),
                    timeout=self.config.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    async def poll_once(self) -> list[dict[str, Any]]:
        discovered: list[dict[str, Any]] = []
        for subscription in await self.repository.subscriptions():
            try:
                discovered.extend(await self._poll_subscription(subscription))
            except Exception as exc:
                state = await self.repository.fetch_state(subscription["subscription_id"])
                state.update({
                    "last_checked_at": datetime.now(UTC).isoformat(),
                    "last_error": f"{type(exc).__name__}: {exc}",
                })
                await self.repository.update_fetch_state(subscription["subscription_id"], state)
        if discovered:
            await self.repository.append_pending(discovered)
        await self._deliver_pending()
        return discovered

    async def _poll_subscription(self, subscription: dict[str, Any]) -> list[dict[str, Any]]:
        subscription_id = subscription["subscription_id"]
        previous = await self.repository.fetch_state(subscription_id)
        result = await self.ctx.resources.tool.call("feed_fetch", {
            "url": subscription["url"],
            "if_none_match": str(previous.get("etag") or ""),
            "if_modified_since": str(previous.get("last_modified") or ""),
        })
        if str(getattr(result, "status", "")) != "success":
            raise RuntimeError(str(getattr(result, "error", "") or getattr(result, "content", "")))
        payload = json.loads(str(getattr(result, "content", "") or "{}"))
        if payload.get("not_modified"):
            previous.update({"last_checked_at": datetime.now(UTC).isoformat(), "last_error": ""})
            await self.repository.update_fetch_state(subscription_id, previous)
            return []
        entries = _parse_feed(str(payload.get("content") or ""), limit=self.config.max_items_per_feed)
        seen = set(str(value) for value in previous.get("seen_ids", []))
        first_run = not bool(previous)
        fresh = [item for item in entries if item["entry_id"] not in seen and _matches(item, subscription["keywords"])]
        state = {
            "etag": str(payload.get("etag") or ""),
            "last_modified": str(payload.get("last_modified") or ""),
            "seen_ids": list(dict.fromkeys([item["entry_id"] for item in entries] + list(seen)))[:500],
            "last_checked_at": datetime.now(UTC).isoformat(),
            "last_error": "",
        }
        await self.repository.update_fetch_state(subscription_id, state)
        if first_run:
            return []
        return [_feed_event(subscription, item) for item in fresh]

    async def _deliver_pending(self) -> None:
        pending = await self.repository.pending()
        if not pending:
            return
        by_session: dict[str, list[dict[str, Any]]] = {}
        for item in pending:
            sessions = item.get("sessions") or self.config.active.sessions
            for session_key in sessions:
                if session_key in self.config.active.sessions or "*" in self.config.active.sessions:
                    by_session.setdefault(session_key, []).append(item)
        if not by_session:
            return
        delivered_ids: set[str] = set()
        for session_key, entries in by_session.items():
            digest = hashlib.sha256(
                json.dumps([item["event_id"] for item in entries], sort_keys=True).encode()
            ).hexdigest()[:20]
            handle = await self.ctx.resources.conversation.submit(
                session_key=session_key,
                text=(
                    "Feed Watch 发现以下订阅更新。请按来源分组，用简洁中文总结，保留重要标题和链接；"
                    "不要声称访问了未提供的正文。\n\n"
                    + json.dumps(entries, ensure_ascii=False, indent=2)
                ),
                request_id=f"feed-watch:{digest}:{hashlib.sha256(session_key.encode()).hexdigest()[:8]}",
                metadata={"plugin": "feed-watch", "feed_event_ids": [item["event_id"] for item in entries]},
            )
            outcome_method = getattr(handle, "outcome", None)
            if callable(outcome_method):
                outcome = await outcome_method()
                if not bool(getattr(outcome, "succeeded", False)):
                    continue
            delivered_ids.update(item["event_id"] for item in entries)
        if delivered_ids:
            await self.repository.clear_pending(delivered_ids)


async def _feed_fetch(
    url: str,
    if_none_match: str = "",
    if_modified_since: str = "",
    *,
    trusted_private_hosts: frozenset[str] = frozenset(),
) -> str:
    current = str(url or "").strip()
    if not current:
        raise ValueError("feed URL must not be empty")
    headers = {"User-Agent": "Lumora-Feed-Watch/1.0", "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"}
    if if_none_match:
        headers["If-None-Match"] = str(if_none_match)
    if if_modified_since:
        headers["If-Modified-Since"] = str(if_modified_since)
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        for redirect_count in range(_MAX_REDIRECTS + 1):
            error = await asyncio.to_thread(
                _check_feed_url,
                current,
                trusted_private_hosts,
            )
            if error:
                raise PermissionError(error)
            async with client.stream("GET", current, headers=headers) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirect_count >= _MAX_REDIRECTS:
                        raise RuntimeError("feed redirect limit exceeded")
                    location = str(response.headers.get("location") or "")
                    if not location:
                        raise RuntimeError("feed redirect has no location")
                    current = urljoin(current, location)
                    continue
                if response.status_code == 304:
                    return json.dumps({"not_modified": True, "url": current})
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > _MAX_FEED_BYTES:
                        raise ValueError("feed exceeds maximum response size")
                content = bytes(data).decode(response.encoding or "utf-8", errors="replace")
                return json.dumps({
                    "not_modified": False,
                    "url": current,
                    "etag": str(response.headers.get("etag") or ""),
                    "last_modified": str(response.headers.get("last-modified") or ""),
                    "content": content,
                })
    raise RuntimeError("feed redirect handling failed")


def register(ctx) -> None:
    config = ctx.parse_config(FeedWatchConfig)
    trusted_private_hosts = frozenset(
        str(host).strip().lower().rstrip(".")
        for host in config.trusted_private_hosts
        if str(host).strip()
    )
    repository_ref: dict[str, FeedRepository] = {}

    def repository() -> FeedRepository:
        if "value" not in repository_ref:
            repository_ref["value"] = FeedRepository(ctx.storage, config)
        return repository_ref["value"]

    async def add_feed(name: str, url: str, keywords: list[str] | None = None) -> str:
        session_key = _current_session_key()
        if not _session_allowed(session_key, config.active.sessions):
            raise PermissionError("feed session is not allowed by plugin configuration")
        error = await asyncio.to_thread(_check_feed_url, url, trusted_private_hosts)
        if error:
            raise PermissionError(error)
        item = await repository().add(name=name, url=url, keywords=list(keywords or []), session_key=session_key)
        return json.dumps(item, ensure_ascii=False)

    async def remove_feed(subscription_id: str) -> str:
        removed = await repository().remove(subscription_id, session_key=_current_session_key())
        if not removed:
            raise KeyError(f"feed subscription not found: {subscription_id}")
        return json.dumps({"subscription_id": subscription_id, "removed": True})

    async def list_feeds() -> str:
        return json.dumps(await repository().subscriptions(session_key=_current_session_key()), ensure_ascii=False)

    async def fetch_feed(
        url: str,
        if_none_match: str = "",
        if_modified_since: str = "",
    ) -> str:
        return await _feed_fetch(
            url,
            if_none_match,
            if_modified_since,
            trusted_private_hosts=trusted_private_hosts,
        )

    ctx.register.tool(ToolEntry(
        name="feed_fetch",
        description="Fetch raw RSS or Atom content with optional conditional request headers.",
        schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "if_none_match": {"type": "string"},
                "if_modified_since": {"type": "string"},
            },
            "required": ["url"],
        },
        handler=fetch_feed,
        toolset="web",
        permission_category="network",
        tags=["network", "feed", "fetch"],
        timeout_seconds=35,
    ))
    ctx.register.tool(ToolEntry(
        "feed_add", "Add an RSS or Atom subscription for the current session.",
        {"type": "object", "properties": {
            "name": {"type": "string"}, "url": {"type": "string"},
            "keywords": {"type": "array", "items": {"type": "string"}},
        }, "required": ["name", "url"]},
        add_feed, toolset="productivity", idempotent=False, is_parallel_safe=False,
    ))
    ctx.register.tool(ToolEntry(
        "feed_remove", "Remove one feed subscription for the current session.",
        {"type": "object", "properties": {"subscription_id": {"type": "string"}}, "required": ["subscription_id"]},
        remove_feed, toolset="productivity", idempotent=True, is_parallel_safe=False,
    ))
    ctx.register.tool(ToolEntry(
        "feed_list", "List feed subscriptions for the current session.",
        {"type": "object", "properties": {}}, list_feeds,
        toolset="productivity", permission_category="read", tags=["feed", "read"],
    ))

    async def feeds_command(args="", **kwargs):
        items = await repository().subscriptions(session_key=str(kwargs.get("session_key") or ""))
        if not items:
            return "当前会话没有 Feed 订阅。"
        return "Feed 订阅:\n" + "\n".join(
            f"- {item['subscription_id']} {item['name']}: {item['url']}" for item in items
        )

    ctx.register.command(CommandEntry("feeds", "List feed subscriptions.", feeds_command, scope="both"))

    async def run(active_ctx) -> None:
        await FeedWatcher(active_ctx, config, repository()).run()

    ctx.register.active(
        run=run,
        resources=ActiveResourceRequest(tools=("feed_fetch",), conversation=True),
        restart_policy="on_failure",
        startup_timeout=15,
        shutdown_timeout=15,
    )


def _check_feed_url(url: str, trusted_private_hosts: frozenset[str]) -> str | None:
    hostname = str(urlparse(str(url or "")).hostname or "").lower().rstrip(".")
    return check_url(url, allow_private=bool(hostname and hostname in trusted_private_hosts))


def _subscription(name: str, url: str, keywords: list[str], sessions: list[str], source: str) -> dict[str, Any]:
    normalized_url = str(url or "").strip()
    if urlparse(normalized_url).scheme not in {"http", "https"}:
        raise ValueError("feed URL must use http or https")
    subscription_id = "feed_" + hashlib.sha256(normalized_url.encode()).hexdigest()[:16]
    return {
        "subscription_id": subscription_id,
        "name": str(name or normalized_url).strip(),
        "url": normalized_url,
        "keywords": [str(value).strip() for value in keywords if str(value).strip()],
        "sessions": list(dict.fromkeys(str(value) for value in sessions if str(value))),
        "source": source,
    }


def _parse_feed(content: str, *, limit: int) -> list[dict[str, Any]]:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ValueError("invalid RSS or Atom XML") from exc
    entries = []
    candidates = root.findall(".//item")
    if not candidates:
        candidates = root.findall(".//{*}entry")
    for element in candidates[:limit]:
        title = _child_text(element, "title")
        link = _entry_link(element)
        identifier = _child_text(element, "guid") or _child_text(element, "id") or link
        summary = _child_text(element, "description") or _child_text(element, "summary") or _child_text(element, "content")
        published = _child_text(element, "pubDate") or _child_text(element, "published") or _child_text(element, "updated")
        stable = identifier or hashlib.sha256(f"{title}:{published}:{link}".encode()).hexdigest()
        entries.append({
            "entry_id": hashlib.sha256(stable.encode()).hexdigest()[:24],
            "title": _clean_text(title)[:500],
            "url": link,
            "summary": _clean_text(summary)[:1000],
            "published_at": published,
        })
    return entries


def _child_text(element, name: str) -> str:
    child = element.find(name)
    if child is None:
        child = element.find(f"{{*}}{name}")
    return "" if child is None else "".join(child.itertext()).strip()


def _entry_link(element) -> str:
    child = element.find("link")
    if child is None:
        child = element.find("{*}link")
    if child is None:
        return ""
    return str(child.attrib.get("href") or child.text or "").strip()


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))).strip()


def _matches(item: dict[str, Any], keywords: list[str]) -> bool:
    if not keywords:
        return True
    haystack = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    return any(str(keyword).lower() in haystack for keyword in keywords)


def _feed_event(subscription: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    identity = f"{subscription['subscription_id']}:{item['entry_id']}"
    return {
        "event_id": hashlib.sha256(identity.encode()).hexdigest()[:24],
        "subscription_id": subscription["subscription_id"],
        "feed_name": subscription["name"],
        "sessions": list(subscription.get("sessions") or []),
        **item,
    }


def _current_session_key() -> str:
    agent = current_tool_agent()
    security = getattr(agent, "_security_context", None)
    value = str(getattr(security, "session_key", "") or getattr(agent, "_memory_session_key", "") or "")
    if not value:
        raise RuntimeError("current session is unavailable")
    return value


def _session_allowed(session_key: str, allowed: list[str]) -> bool:
    return bool(session_key and ("*" in allowed or session_key in allowed))
