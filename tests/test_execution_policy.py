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


def test_execution_policy_unknown_mode_falls_back_to_standard():
    from personal_agent.execution import resolve_execution_policy

    policy = resolve_execution_policy(SimpleNamespace(execution_mode="wat", bash_allow_network=False))

    assert policy.mode == "standard"
    assert policy.permission_for("write") == "ask"
