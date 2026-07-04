from __future__ import annotations

from types import SimpleNamespace


def test_execution_policy_defaults_to_standard():
    from personal_agent.execution import resolve_execution_policy

    policy = resolve_execution_policy(SimpleNamespace(bash_allow_network=False))

    assert policy.mode == "standard"
    assert policy.permission_for("read") == "allow"
    assert policy.permission_for("write") == "ask"
    assert policy.permission_for("bash") == "ask"
    assert policy.network == "deny"
    assert policy.isolation == "tool-enforced"
    assert policy.profile is not None
    assert policy.profile.label == "Standard"
    assert policy.profile.sandbox.hard_prechecks_enforced is True


def test_execution_policy_modes_are_stable():
    from personal_agent.execution import resolve_execution_policy

    guarded = resolve_execution_policy(SimpleNamespace(execution_mode="guarded", bash_allow_network=True))
    trusted = resolve_execution_policy(SimpleNamespace(execution_mode="trusted", bash_allow_network=False))
    sovereign = resolve_execution_policy(SimpleNamespace(execution_mode="sovereign", bash_allow_network=True))

    assert guarded.permission_for("bash") == "deny"
    assert guarded.network == "deny"
    assert trusted.permission_for("write") == "allow"
    assert trusted.network == "ask"
    assert sovereign.permission_for("bash") == "allow"
    assert sovereign.network == "allow"
    assert sovereign.warnings


def test_execution_policy_as_dict_includes_profile_sections():
    from personal_agent.execution import resolve_execution_policy

    policy = resolve_execution_policy(SimpleNamespace(execution_mode="trusted", bash_allow_network=False))
    data = policy.as_dict()

    assert data["profile"]["name"] == "trusted"
    assert data["profile"]["label"] == "Trusted"
    assert data["profile"]["tool_permissions"]["bash"] == "allow"
    assert data["profile"]["sandbox"]["path_roots_enforced"] is True
    assert data["profile"]["network"]["tool_permission"] == "ask"
    assert data["profile"]["grants"]["scope"] == "turn"
    assert "bash" in data["profile"]["grants"]["categories"]
    assert data["profile"]["audit"]["decisions"] is True
    assert data["overrides"]["tool_permissions"] == {}


def test_execution_policy_explains_permission_decisions():
    from personal_agent.execution import resolve_execution_policy

    standard = resolve_execution_policy(SimpleNamespace(execution_mode="standard", bash_allow_network=False))
    guarded = resolve_execution_policy(SimpleNamespace(execution_mode="guarded", bash_allow_network=False))
    trusted = resolve_execution_policy(SimpleNamespace(execution_mode="trusted", bash_allow_network=False))

    ask = standard.explain_permission("bash")
    deny = guarded.explain_permission("bash")
    allow = trusted.explain_permission("bash")

    assert ask["decision"] == "ask"
    assert ask["required_allow"] == "bash"
    assert "/allow bash" in ask["message"]
    assert deny["decision"] == "deny"
    assert "denied by execution mode" in deny["message"]
    assert allow["decision"] == "allow"


def test_execution_policy_unknown_mode_falls_back_to_standard():
    from personal_agent.execution import resolve_execution_policy

    policy = resolve_execution_policy(SimpleNamespace(execution_mode="wat", bash_allow_network=False))

    assert policy.mode == "standard"
    assert policy.permission_for("write") == "ask"


def test_execution_policy_accepts_flat_permission_overrides():
    from personal_agent.execution import resolve_execution_policy

    policy = resolve_execution_policy(SimpleNamespace(
        execution_mode="standard",
        bash_allow_network=False,
        execution_policy_overrides={"background": "allow", "network": "ask"},
    ))
    data = policy.as_dict()

    assert policy.permission_for("read") == "allow"
    assert policy.permission_for("background") == "allow"
    assert policy.permission_for("network") == "ask"
    assert data["overrides"]["tool_permissions"] == {
        "background": "allow",
        "network": "ask",
    }


def test_execution_policy_accepts_nested_permission_overrides():
    from personal_agent.execution import resolve_execution_policy

    policy = resolve_execution_policy(SimpleNamespace(
        execution_mode="trusted",
        bash_allow_network=False,
        execution_policy_overrides={"tool_permissions": {"bash": "ask"}},
    ))

    assert policy.permission_for("write") == "allow"
    assert policy.permission_for("bash") == "ask"
    assert policy.as_dict()["overrides"]["tool_permissions"] == {"bash": "ask"}


def test_execution_policy_ignores_invalid_runtime_overrides():
    from personal_agent.execution import resolve_execution_policy

    policy = resolve_execution_policy(SimpleNamespace(
        execution_mode="standard",
        bash_allow_network=False,
        execution_policy_overrides={
            "sandbox": {"path_roots_enforced": False},
            "background": "maybe",
            "unknown": "allow",
            "tool_permissions": "bad",
        },
    ))

    assert policy.permission_for("background") == "ask"
    assert policy.permission_for("write") == "ask"
    assert policy.as_dict()["overrides"]["tool_permissions"] == {}
