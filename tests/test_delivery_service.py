from types import SimpleNamespace

import pytest

from luna_agent.conversation import SessionDirectory
from luna_agent.delivery import (
    DeliveryKind,
    DeliveryRequest,
    DeliveryService,
    DeliveryStatus,
    PlatformDirectory,
)
from luna_agent.hooks import HookEvent, HookManager, PreDeliveryOutcome
from luna_agent.models.messages import OutboundMessage, SessionSource


class Adapter:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, message):
        self.sent.append((chat_id, message.render_text()))
        return SimpleNamespace(success=True, message_id="m1", error="")


def _runtime(hook_manager=None):
    sessions = SessionDirectory()
    sessions.active_key(SessionSource(platform="wechat", user_id="u1", chat_id="c1"))
    platforms = PlatformDirectory()
    adapter = Adapter()
    platforms.register("wechat", adapter)
    return DeliveryService(sessions=sessions, platforms=platforms, hook_manager=hook_manager), adapter


@pytest.mark.asyncio
async def test_delivery_resolves_session_and_sends_once():
    service, adapter = _runtime()
    result = await service.deliver(DeliveryRequest(
        session_key="wechat:c1:u1",
        message=OutboundMessage.text("hello"),
    ))

    assert result.status == DeliveryStatus.DELIVERED
    assert result.message_id == "m1"
    assert result.attempts == 1
    assert adapter.sent == [("c1", "hello")]


@pytest.mark.asyncio
async def test_delivery_hook_can_replace_or_suppress_normal_message():
    hooks = HookManager()

    async def replace(event):
        if event.payload["text"] == "hide":
            return PreDeliveryOutcome.suppress("hidden")
        return PreDeliveryOutcome.replace_text("changed")

    hooks.register(owner="test", event=HookEvent.PRE_DELIVERY, callback=replace)
    service, adapter = _runtime(hooks)

    changed = await service.deliver(DeliveryRequest(
        session_key="wechat:c1:u1",
        message=OutboundMessage.text("hello"),
    ))
    hidden = await service.deliver(DeliveryRequest(
        session_key="wechat:c1:u1",
        message=OutboundMessage.text("hide"),
    ))

    assert changed.delivered
    assert hidden.status == DeliveryStatus.SUPPRESSED
    assert adapter.sent == [("c1", "changed")]


@pytest.mark.asyncio
async def test_protected_delivery_cannot_be_suppressed_by_plugin_hook():
    hooks = HookManager()
    hooks.register(
        owner="test",
        event=HookEvent.PRE_DELIVERY,
        callback=lambda event: PreDeliveryOutcome.suppress("blocked"),
    )
    service, adapter = _runtime(hooks)

    result = await service.deliver(DeliveryRequest(
        session_key="wechat:c1:u1",
        message=OutboundMessage.text("approval required"),
        kind=DeliveryKind.APPROVAL,
    ))

    assert result.delivered
    assert adapter.sent == [("c1", "approval required")]


@pytest.mark.asyncio
async def test_unavailable_platform_is_deferred():
    sessions = SessionDirectory()
    sessions.active_key(SessionSource(platform="offline", user_id="u1", chat_id="c1"))
    service = DeliveryService(sessions=sessions, platforms=PlatformDirectory())

    result = await service.deliver(DeliveryRequest(
        session_key="offline:c1:u1",
        message=OutboundMessage.text("later"),
    ))

    assert result.status == DeliveryStatus.DEFERRED


@pytest.mark.asyncio
async def test_missing_delivery_binding_is_deferred():
    service = DeliveryService(sessions=SessionDirectory(), platforms=PlatformDirectory())

    result = await service.deliver(DeliveryRequest(
        session_key="wechat:c1:u1",
        message=OutboundMessage.text("later"),
    ))

    assert result.status == DeliveryStatus.DEFERRED
    assert result.error == "session has no delivery binding"


@pytest.mark.asyncio
async def test_partial_platform_delivery_is_ambiguous():
    service, adapter = _runtime()

    async def partial_send(chat_id, message):
        return SimpleNamespace(
            success=False,
            message_id="",
            error="partial delivery: second chunk failed",
        )

    adapter.send_message = partial_send
    result = await service.deliver(DeliveryRequest(
        session_key="wechat:c1:u1",
        message=OutboundMessage.text("long message"),
    ))

    assert result.status == DeliveryStatus.FAILED
    assert result.ambiguous is True
