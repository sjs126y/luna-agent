from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from luna_agent.config import Settings
from luna_agent.plugins import PluginManager, PluginStatus


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "feed_watch"
PLUGIN_KEY = "automation/feed-watch"


def _manager(tmp_path) -> PluginManager:
    settings = Settings(
        agent_data_dir=tmp_path / "data",
        plugins_dirs=[PLUGIN_ROOT],
        plugins_enabled=[PLUGIN_KEY],
        plugins_config={
            PLUGIN_KEY: {
                "feeds": [{"name": "Example", "url": "https://example.com/feed.xml"}],
                "active": {"enabled": False, "sessions": ["wechat:c1:u1"]},
            }
        },
        mcp_enabled=False,
        memory_external_provider="none",
    )
    manager = PluginManager(
        settings,
        plugin_dirs=[PLUGIN_ROOT],
        state_path=tmp_path / "plugin-state.json",
        include_builtin=False,
    )
    manager.load_enabled()
    return manager


def test_feed_watch_registers_tools_command_and_active_runner(tmp_path):
    manager = _manager(tmp_path)
    plugin = manager.list_plugins()[0]

    assert plugin.status is PluginStatus.LOADED
    assert set(plugin.tools_registered) == {"feed_fetch", "feed_add", "feed_remove", "feed_list"}
    assert plugin.commands_registered == ["feeds"]
    assert plugin.active_registration.resources.tools == ("feed_fetch",)
    assert plugin.active_registration.resources.conversation is True
    manager.unload_plugin(plugin.key)


def test_feed_parser_supports_rss_and_atom(tmp_path):
    manager = _manager(tmp_path)
    module = manager.list_plugins()[0].module
    rss = module._parse_feed(_rss(("one", "First")), limit=10)
    atom = module._parse_feed(
        """<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>a1</id><title>Atom</title>
        <link href="https://example.com/a1"/><summary>Hello</summary></entry></feed>""",
        limit=10,
    )

    assert rss[0]["title"] == "First"
    assert atom[0]["title"] == "Atom"
    assert atom[0]["url"] == "https://example.com/a1"


def test_feed_private_dns_requires_exact_trusted_host(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    module = manager.list_plugins()[0].module
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ],
    )

    assert module._check_feed_url(
        "https://github.com/openai/codex/releases.atom",
        frozenset({"github.com"}),
    ) is None
    assert module._check_feed_url(
        "https://untrusted.example/feed.xml",
        frozenset({"github.com"}),
    ) is not None

    manager.unload_plugin(PLUGIN_KEY)


@pytest.mark.asyncio
async def test_feed_watch_baselines_then_delivers_new_entry(tmp_path):
    manager = _manager(tmp_path)
    plugin = manager.list_plugins()[0]
    module = plugin.module
    config = module.FeedWatchConfig.model_validate({
        "feeds": [{"name": "Example", "url": "https://example.com/feed.xml"}],
        "active": {"sessions": ["wechat:c1:u1"]},
    })
    storage = _Storage()
    repository = module.FeedRepository(storage, config)
    tool = _Tool([
        _fetch(_rss(("one", "First")), etag="v1"),
        _fetch(_rss(("two", "Second"), ("one", "First")), etag="v2"),
    ])
    conversation = _Conversation()
    ctx = SimpleNamespace(resources=SimpleNamespace(tool=tool, conversation=conversation))
    watcher = module.FeedWatcher(ctx, config, repository)

    assert await watcher.poll_once() == []
    events = await watcher.poll_once()

    assert len(events) == 1
    assert events[0]["title"] == "Second"
    assert tool.calls[1]["if_none_match"] == "v1"
    assert len(conversation.requests) == 1
    assert await repository.pending() == []
    manager.unload_plugin(plugin.key)


def _rss(*items):
    body = "".join(
        f"<item><guid>{identifier}</guid><title>{title}</title>"
        f"<link>https://example.com/{identifier}</link><description>Body</description></item>"
        for identifier, title in items
    )
    return f"<rss><channel>{body}</channel></rss>"


def _fetch(content, *, etag):
    return json.dumps({
        "not_modified": False,
        "url": "https://example.com/feed.xml",
        "etag": etag,
        "last_modified": "",
        "content": content,
    })


class _Storage:
    def __init__(self):
        self.values = {}

    def read_json(self, path, *, default=None, schema_version=None):
        return self.values.get(str(path), default)

    def write_json_atomic(self, path, value):
        self.values[str(path)] = value


class _Tool:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    async def call(self, name, arguments):
        assert name == "feed_fetch"
        self.calls.append(dict(arguments))
        return SimpleNamespace(status="success", content=self.payloads.pop(0), error="")


class _Conversation:
    def __init__(self):
        self.requests = []

    async def submit(self, **kwargs):
        self.requests.append(kwargs)
        return "accepted"
