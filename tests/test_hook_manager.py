import asyncio

import pytest

from personal_agent.hooks import (
    GatewayMessageOutcome,
    HookEnvelope,
    HookEvent,
    HookManager,
    HookScope,
    HookSourceContext,
    PermissionDecision,
    PermissionRequestOutcome,
    PreDeliveryOutcome,
    PreToolUseOutcome,
)


def _envelope(event: HookEvent, *, payload=None, platform="wechat") -> HookEnvelope:
    return HookEnvelope(
        event_name=event,
        scope=HookScope.TURN,
        session_key="wechat:chat:user",
        turn_id="turn-1",
        cwd="/workspace",
        mode="ask-first",
        source=HookSourceContext(platform=platform, user_id="user", chat_id="chat"),
        payload=payload or {},
    )


def test_hook_envelope_serializes_stable_wire_shape():
    data = _envelope(HookEvent.PRE_TOOL_USE, payload={"tool_name": "write"}).to_dict()

    assert data["schema_version"] == 1
    assert data["event_name"] == "PreToolUse"
    assert data["scope"] == "turn"
    assert data["source"]["platform"] == "wechat"
    assert data["payload"]["tool_name"] == "write"


def test_register_rejects_invalid_matcher():
    manager = HookManager()

    with pytest.raises(ValueError, match="Invalid hook matcher"):
        manager.register(
            owner="plugin/demo",
            event=HookEvent.PRE_TOOL_USE,
            callback=lambda event: None,
            matcher="[",
        )


@pytest.mark.asyncio
async def test_gateway_message_pipeline_passes_updated_text_to_next_hook():
    manager = HookManager()
    seen = []

    async def first(event):
        return GatewayMessageOutcome.replace_message(text=event.payload["text"] + "-first")

    async def second(event):
        seen.append(event.payload["text"])
        return GatewayMessageOutcome.replace_message(text=event.payload["text"] + "-second")

    manager.register(owner="a", event=HookEvent.GATEWAY_MESSAGE_RECEIVED, callback=second, priority=20)
    manager.register(owner="b", event=HookEvent.GATEWAY_MESSAGE_RECEIVED, callback=first, priority=10)

    outcome = await manager.dispatch(
        _envelope(HookEvent.GATEWAY_MESSAGE_RECEIVED, payload={"text": "hello"})
    )

    assert seen == ["hello-first"]
    assert outcome.text == "hello-first-second"


@pytest.mark.asyncio
async def test_gateway_message_pipeline_preserves_changes_from_multiple_hooks():
    manager = HookManager()

    manager.register(
        owner="text",
        event=HookEvent.GATEWAY_MESSAGE_RECEIVED,
        callback=lambda event: GatewayMessageOutcome.replace_message(text="changed"),
        priority=10,
    )
    manager.register(
        owner="metadata",
        event=HookEvent.GATEWAY_MESSAGE_RECEIVED,
        callback=lambda event: GatewayMessageOutcome.replace_message(metadata={"reviewed": True}),
        priority=20,
    )

    outcome = await manager.dispatch(
        _envelope(HookEvent.GATEWAY_MESSAGE_RECEIVED, payload={"text": "hello", "metadata": {}})
    )

    assert outcome.text == "changed"
    assert outcome.metadata == {"reviewed": True}


@pytest.mark.asyncio
async def test_delivery_matcher_filters_platform():
    manager = HookManager()
    calls = 0

    async def callback(event):
        nonlocal calls
        calls += 1
        return PreDeliveryOutcome.replace_text("changed")

    manager.register(
        owner="wechat-only",
        event=HookEvent.PRE_DELIVERY,
        callback=callback,
        matcher="^wechat$",
    )

    qq = await manager.dispatch(
        _envelope(HookEvent.PRE_DELIVERY, payload={"text": "same"}, platform="qq")
    )
    wechat = await manager.dispatch(
        _envelope(HookEvent.PRE_DELIVERY, payload={"text": "same"})
    )

    assert qq.text is None
    assert wechat.text == "changed"
    assert calls == 1


@pytest.mark.asyncio
async def test_pre_tool_use_any_block_wins_and_rewrite_is_ignored():
    manager = HookManager()

    async def rewrite(event):
        return PreToolUseOutcome(updated_input={"path": "rewritten"})

    async def block(event):
        return PreToolUseOutcome.block("protected")

    manager.register(owner="rewrite", event=HookEvent.PRE_TOOL_USE, callback=rewrite, priority=10)
    manager.register(owner="block", event=HookEvent.PRE_TOOL_USE, callback=block, priority=20)

    outcome = await manager.dispatch(
        _envelope(
            HookEvent.PRE_TOOL_USE,
            payload={"tool_name": "write", "tool_input": {"path": "original"}},
        )
    )

    assert outcome.blocked is True
    assert outcome.reason == "protected"
    assert outcome.updated_input is None


@pytest.mark.asyncio
async def test_permission_request_deny_wins_over_allow():
    manager = HookManager()

    manager.register(
        owner="allow",
        event=HookEvent.PERMISSION_REQUEST,
        callback=lambda event: PermissionRequestOutcome(PermissionDecision.ALLOW),
    )
    manager.register(
        owner="deny",
        event=HookEvent.PERMISSION_REQUEST,
        callback=lambda event: PermissionRequestOutcome(PermissionDecision.DENY, "policy"),
    )

    outcome = await manager.dispatch(
        _envelope(HookEvent.PERMISSION_REQUEST, payload={"tool_name": "bash"})
    )

    assert outcome.decision == PermissionDecision.DENY
    assert outcome.reason == "policy"


@pytest.mark.asyncio
async def test_pre_tool_timeout_fails_closed():
    manager = HookManager()

    async def slow(event):
        await asyncio.sleep(0.05)

    manager.register(
        owner="slow",
        event=HookEvent.PRE_TOOL_USE,
        callback=slow,
        timeout_seconds=0.01,
    )

    outcome = await manager.dispatch(
        _envelope(HookEvent.PRE_TOOL_USE, payload={"tool_name": "bash"})
    )
    health = manager.health_snapshot()

    assert outcome.blocked is True
    assert "timed out" in outcome.reason
    assert health["items"][0]["timeout_count"] == 1


@pytest.mark.asyncio
async def test_unregister_owner_removes_all_hooks():
    manager = HookManager()
    manager.register(owner="plugin/a", event=HookEvent.GATEWAY_START, callback=lambda event: None)
    manager.register(owner="plugin/a", event=HookEvent.GATEWAY_STOP, callback=lambda event: None)

    removed = manager.unregister_owner("plugin/a")

    assert len(removed) == 2
    assert manager.registrations() == []
    assert manager.health_snapshot()["registered"] == 0
