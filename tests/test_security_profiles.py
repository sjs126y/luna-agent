from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def _settings(tmp_path: Path, *, mode: str = "ask-first"):
    return SimpleNamespace(
        execution_mode=mode,
        sandbox_roots=[tmp_path],
        sandbox_read_roots=[],
        permission_grant_ttl_minutes=30,
    )


def test_mode_presets_have_one_stable_mapping():
    from personal_agent.security.modes import mode_preset

    assert mode_preset("read-only").profile == "read-only"
    assert mode_preset("ask-first").approval_policy == "on-request"
    assert mode_preset("local-auto").profile == "workspace"
    assert mode_preset("full-auto").profile == "trusted"
    assert mode_preset("standard").id == "ask-first"


def test_permission_profiles_enforce_actual_roots(tmp_path):
    from personal_agent.security.models import ResourceRequirement
    from personal_agent.security.session import SecurityStateStore

    inside = ResourceRequirement("filesystem", str(tmp_path / "a.txt"), "write")
    outside = ResourceRequirement("filesystem", str(tmp_path.parent / "other.txt"), "read")

    ask_first = SecurityStateStore(_settings(tmp_path, mode="ask-first")).context("s")
    local_auto = SecurityStateStore(_settings(tmp_path, mode="local-auto")).context("s")

    assert ask_first.profile.allows(inside) is False
    assert local_auto.profile.allows(inside) is True
    assert local_auto.profile.allows(outside) is False
    assert ask_first.profile.network_enabled is False


def test_local_auto_supports_additional_read_only_roots(tmp_path):
    from personal_agent.security.models import ResourceRequirement
    from personal_agent.security.session import SecurityStateStore

    home = tmp_path / "home"
    workspace = home / "projects" / "agent"
    outside = tmp_path / "outside"
    workspace.mkdir(parents=True)
    outside.mkdir()
    settings = _settings(workspace, mode="local-auto")
    settings.sandbox_read_roots = [home]
    context = SecurityStateStore(settings).context("wechat:user")

    assert context.profile.allows(
        ResourceRequirement("filesystem", str(home / "notes.txt"), "read")
    )
    assert not context.profile.allows(
        ResourceRequirement("filesystem", str(home / "notes.txt"), "write")
    )
    assert context.profile.allows(
        ResourceRequirement("filesystem", str(workspace / "code.py"), "write")
    )
    assert not context.profile.allows(
        ResourceRequirement("filesystem", str(outside / "file.txt"), "read")
    )


def test_session_grants_use_one_ttl_and_mode_switch_clears(tmp_path):
    from personal_agent.security.models import ResourceRequirement
    from personal_agent.security.session import SecurityStateStore

    store = SecurityStateStore(_settings(tmp_path))
    state = store.get("wechat:user")
    resource = ResourceRequirement("network", "https://api.github.com:443", "connect")

    tool_expiry = state.grant_tool("mcp:github:get", ttl_seconds=store.grant_ttl_seconds, now=100)
    resource_expiry = state.grant_resource(resource, ttl_seconds=store.grant_ttl_seconds, now=100)

    assert tool_expiry == resource_expiry == 1900
    assert state.has_tool_grant("mcp:github:get", now=101)
    assert state.has_resource_grant(resource, now=101)

    switched = store.set_mode("wechat:user", "local-auto")
    assert switched.mode_id == "local-auto"
    assert switched.revision == 1
    assert switched.tool_grants == {}
    assert switched.resource_grants == {}


def test_permission_helpers_use_unified_grant_ttl():
    from personal_agent.permissions import format_grant_duration, temporary_grant_ttl_seconds

    settings = SimpleNamespace(
        permission_grant_ttl_minutes=90,
    )

    assert temporary_grant_ttl_seconds(settings) == 90 * 60
    assert format_grant_duration(90 * 60) == "90分钟"


def test_security_state_is_not_shared_between_sessions(tmp_path):
    from personal_agent.security.session import SecurityStateStore

    store = SecurityStateStore(_settings(tmp_path))
    store.get("wechat:a").grant_tool("tool:x", ttl_seconds=60, now=100)

    assert store.get("wechat:a").has_tool_grant("tool:x", now=101)
    assert not store.get("telegram:a").has_tool_grant("tool:x", now=101)
