"""Gateway session routing rules."""

from __future__ import annotations

from types import SimpleNamespace

from luna_agent.gateway.session_router import GatewaySessionRouter


def _source(chat_id: str = "c1"):
    return SimpleNamespace(platform="telegram", chat_id=chat_id, user_id="u1")


def test_gateway_session_router_base_active_and_named_keys():
    router = GatewaySessionRouter()
    source = _source()

    assert router.base_key(source) == "telegram:c1:u1"
    assert router.active_key(source) == "telegram:c1:u1"
    assert router.named_key(source, "work") == "telegram:work:u1"
    assert router.current_for_list(source) == "telegram:c1:u1"


def test_gateway_session_router_switch_rename_and_delete_active_session():
    router = GatewaySessionRouter()
    source = _source()

    switched = router.switch(source, "work")
    router.rename(source, switched, "telegram:renamed:u1")
    fallback = router.delete(source, "telegram:renamed:u1")

    assert switched == "telegram:work:u1"
    assert fallback == "telegram:c1:u1"
    assert router.active_key(source) == "telegram:c1:u1"
    assert router.overrides == {}


def test_gateway_session_router_renames_base_session_to_override():
    router = GatewaySessionRouter()
    source = _source()

    router.rename(source, "telegram:c1:u1", "telegram:renamed:u1")

    assert router.active_key(source) == "telegram:renamed:u1"


def test_gateway_session_router_accepts_initial_overrides():
    router = GatewaySessionRouter({"telegram:c1:u1": "telegram:work:u1"})

    assert router.active_key(_source()) == "telegram:work:u1"
    assert router.active_key(_source("c2")) == "telegram:c2:u1"
