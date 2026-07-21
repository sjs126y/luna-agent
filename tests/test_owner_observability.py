import json
from types import SimpleNamespace

import pytest

from luna_agent.gateway.auth import OwnerAccessPolicy
from luna_agent.memory.archive import MemoryArchive
from luna_agent.memory.models import MemoryRecord, MemoryScope
from luna_agent.tools import audit
from luna_agent.trace import context, current_context


def _source(platform="telegram", user_id="owner", chat_type="dm"):
    return SimpleNamespace(platform=platform, user_id=user_id, chat_type=chat_type)


def test_owner_policy_is_platform_scoped_and_rejects_groups():
    policy = OwnerAccessPolicy(SimpleNamespace(
        auth_enabled=True,
        auth_owner_ids={"telegram": ["owner"]},
    ))

    assert policy.is_allowed(_source())
    assert not policy.is_allowed(_source(user_id="other"))
    assert not policy.is_allowed(_source(chat_type="group"))
    assert policy.is_allowed(_source(platform="cli", user_id="any"))


@pytest.mark.asyncio
async def test_config_inspect_masks_owner_ids_but_reports_shape(monkeypatch):
    from luna_agent.config import Settings
    from luna_agent.plugins.builtin.tools.builtin import observability_tools

    settings = Settings(auth_owner_ids={"wechat": ["private-owner"]})
    monkeypatch.setattr(
        observability_tools,
        "_port",
        lambda: SimpleNamespace(settings=lambda: settings),
    )

    result = await observability_tools.config_inspect(action="field", key="auth.owner_ids")
    payload = json.loads(result)

    assert payload["ok"] is True
    assert "private-owner" not in result
    assert payload["field"]["value"] == {
        "configured": True,
        "platforms": ["wechat"],
        "owner_count": 1,
    }


def test_unknown_plugin_query_uses_stable_error():
    from luna_agent.plugins.query import PluginNotFoundError, PluginQueryService

    manager = SimpleNamespace(_plugins={}, discover=lambda: None)

    with pytest.raises(PluginNotFoundError, match="Plugin not found"):
        PluginQueryService(manager).plugin_info("invalid/missing")


def test_trace_context_restores_nested_values():
    assert current_context()["session_key"] == ""
    with context(trace_id="trace-1", session_key="telegram:owner", turn_id="turn-1"):
        assert current_context()["trace_id"] == "trace-1"
        assert current_context()["session_key"] == "telegram:owner"
    assert current_context()["trace_id"] == ""


def test_audit_query_is_bounded_and_disabled(tmp_path, monkeypatch):
    path = tmp_path / "audit.log"
    monkeypatch.setattr(audit, "_AUDIT_PATH", path)
    audit.set_audit_enabled(True)
    with context(trace_id="trace-1", session_key="cli:owner", turn_id="turn-1"):
        audit.audit_log("read", "api_key=abc", "ok", True)
    rows = audit.query_audit(trace_id="trace-1", limit=1)
    assert len(rows) == 1
    assert rows[0]["session_key"] == "cli:owner"
    assert "abc" not in rows[0]["detail"]
    audit.set_audit_enabled(False)
    audit.audit_log("read", "later", "ok", True)
    assert len(audit.query_audit(limit=10)) == 1
    audit.set_audit_enabled(True)


@pytest.mark.asyncio
async def test_memory_owner_migration_is_dry_run_first(tmp_path):
    archive = MemoryArchive(tmp_path / "memory.db")
    await archive.initialize()
    source = MemoryScope(agent_id="luna", user_id="legacy-user", profile="luna")
    await archive.upsert_memory(
        source,
        MemoryRecord(id="memory-1", content="prefers concise replies", scope=source),
    )

    preview = await archive.migrate_scope_keys(["luna:legacy-user:luna"])
    assert preview["apply"] is False
    assert preview["targets"] == ["luna:owner:luna"]
    assert "luna:legacy-user:luna" in await archive.scope_keys()

    applied = await archive.migrate_scope_keys(["luna:legacy-user:luna"], apply=True)
    assert applied["moved"] == 1
    assert "luna:owner:luna" in await archive.scope_keys()
    assert "luna:legacy-user:luna" not in await archive.scope_keys()
    await archive.close()
