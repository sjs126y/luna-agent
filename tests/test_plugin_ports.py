from types import SimpleNamespace

import pytest

from personal_agent.delivery import DeliveryResult, DeliveryStatus
from personal_agent.plugins.core.ports import PluginConversationPort, PluginNotificationPort
from personal_agent.plugins.runtime import PluginRuntimeState


def _plugin(*, provides=("active", "notification"), sessions=("wechat:c1:u1",)):
    return SimpleNamespace(
        key="user/reminder",
        enabled=True,
        status=SimpleNamespace(value="loaded"),
        runtime_state=PluginRuntimeState.ACTIVE,
        manifest=SimpleNamespace(name="Reminder", provides=list(provides)),
        ctx=SimpleNamespace(config={"active": {"sessions": list(sessions)}}),
    )


class Coordinator:
    def __init__(self):
        self.requests = []

    async def submit(self, request):
        self.requests.append(request)
        return "handle"


@pytest.mark.asyncio
async def test_plugin_conversation_port_injects_owner_and_origin():
    coordinator = Coordinator()
    port = PluginConversationPort(plugin=_plugin(), coordinator=coordinator)

    handle = await port.submit(session_key="wechat:c1:u1", text="check in")

    request = coordinator.requests[0]
    assert handle == "handle"
    assert request.origin.value == "plugin"
    assert request.owner_id == "user/reminder"
    assert request.metadata["plugin_id"] == "user/reminder"


@pytest.mark.asyncio
async def test_plugin_port_rejects_undeclared_or_unconfigured_session():
    coordinator = Coordinator()
    missing_capability = PluginConversationPort(
        plugin=_plugin(provides=()),
        coordinator=coordinator,
    )
    restricted = PluginConversationPort(plugin=_plugin(), coordinator=coordinator)

    with pytest.raises(PermissionError, match="does not declare"):
        await missing_capability.submit(session_key="wechat:c1:u1", text="no")
    with pytest.raises(PermissionError, match="cannot access"):
        await restricted.submit(session_key="wechat:other:u1", text="no")


@pytest.mark.asyncio
async def test_disabled_plugin_cannot_reuse_old_port():
    plugin = _plugin()
    port = PluginConversationPort(plugin=plugin, coordinator=Coordinator())
    plugin.enabled = False

    with pytest.raises(RuntimeError, match="not active"):
        await port.submit(session_key="wechat:c1:u1", text="no")


@pytest.mark.asyncio
async def test_draining_plugin_cannot_start_new_work():
    plugin = _plugin()
    port = PluginConversationPort(plugin=plugin, coordinator=Coordinator())
    plugin.runtime_state = PluginRuntimeState.DRAINING

    with pytest.raises(RuntimeError, match="not active"):
        await port.submit(session_key="wechat:c1:u1", text="no")


@pytest.mark.asyncio
async def test_notification_port_uses_delivery_without_agent():
    requests = []

    class Delivery:
        async def deliver(self, request):
            requests.append(request)
            return DeliveryResult(
                delivery_id=request.delivery_id,
                session_key=request.session_key,
                status=DeliveryStatus.DELIVERED,
            )

    port = PluginNotificationPort(
        plugin=_plugin(),
        coordinator=Coordinator(),
        delivery_service=Delivery(),
    )
    result = await port.send(session_key="wechat:c1:u1", text="time to study")

    assert result.delivered
    assert requests[0].kind.value == "notification"
    assert requests[0].metadata["plugin_id"] == "user/reminder"
