import asyncio
from types import SimpleNamespace

import pytest

from personal_agent.delivery import DeliveryResult, DeliveryStatus
from personal_agent.plugins.core.ports import (
    PluginConversationPort,
    PluginNotificationPort,
    PluginStoragePort,
    PluginTaskPort,
)
from personal_agent.plugins.runtime import PluginRuntimeState


def _plugin(*, provides=("active", "notification"), sessions=("wechat:c1:u1",)):
    return SimpleNamespace(
        key="user/reminder",
        enabled=True,
        status=SimpleNamespace(value="loaded"),
        runtime_state=PluginRuntimeState.ACTIVE,
        runtime_instance_id="runtime-reminder",
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


def test_plugin_storage_is_scoped_to_plugin_root(tmp_path):
    storage = PluginStoragePort(plugin=_plugin(), root=tmp_path)

    path = storage.write_text("nested/state.txt", "saved")

    assert path == tmp_path / "user__reminder" / "nested" / "state.txt"
    assert storage.read_text("nested/state.txt") == "saved"
    with pytest.raises(ValueError, match="relative"):
        storage.resolve(tmp_path / "outside.txt")
    with pytest.raises(ValueError, match="escapes"):
        storage.resolve("../outside.txt")


def test_plugin_storage_supports_atomic_versioned_json(tmp_path):
    storage = PluginStoragePort(plugin=_plugin(), root=tmp_path)

    storage.write_json_atomic("state.json", {"schema_version": 1, "items": [1]})

    assert storage.exists("state.json")
    assert storage.read_json("state.json", schema_version=1)["items"] == [1]
    with pytest.raises(ValueError, match="schema mismatch"):
        storage.read_json("state.json", schema_version=2)


@pytest.mark.asyncio
async def test_plugin_conversation_port_submits_owned_artifacts_with_stable_request_id(tmp_path):
    artifact = SimpleNamespace(
        artifact_id="art_owned",
        owner_id="user/reminder",
        session_key="wechat:c1:u1",
        kind="file",
        filename="note.txt",
        mime_type="text/plain",
        size_bytes=4,
    )

    class Artifacts:
        async def get(self, artifact_id):
            return artifact if artifact_id == artifact.artifact_id else None

        async def resolve_path(self, ref):
            path = tmp_path / ref.filename
            path.write_text("note", encoding="utf-8")
            return path

    coordinator = Coordinator()
    port = PluginConversationPort(
        plugin=_plugin(),
        coordinator=coordinator,
        artifact_store=Artifacts(),
    )

    await port.submit(
        session_key="wechat:c1:u1",
        text="inspect",
        request_id="plugin-event-1",
        artifact_ids=["art_owned"],
    )

    request = coordinator.requests[0]
    assert request.request_id == "plugin-event-1"
    assert request.input.attachments[0].id == "art_owned"
    assert request.input.attachments[0].local_path.endswith("note.txt")


@pytest.mark.asyncio
async def test_plugin_tasks_require_active_runtime_and_are_tracked():
    plugin = _plugin()
    tasks = {}
    port = PluginTaskPort(plugin=plugin, tasks=tasks)

    task = port.create(asyncio.sleep(0), name="plugin-test")
    await task

    assert task.get_name() == "plugin-test"
    assert not tasks[plugin.runtime_instance_id]

    plugin.runtime_state = PluginRuntimeState.DRAINING
    with pytest.raises(RuntimeError, match="not active"):
        port.create(asyncio.sleep(0))
