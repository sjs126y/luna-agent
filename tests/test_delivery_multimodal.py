from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_agent.artifacts import ArtifactStore
from personal_agent.conversation import SessionDirectory
from personal_agent.db.database import Database
from personal_agent.delivery import (
    DeliveryOutbox,
    DeliveryRequest,
    DeliveryService,
    DeliveryStatus,
    DeliveryWorker,
    PlatformDirectory,
)
from personal_agent.models.messages import (
    MessagePart,
    OutboundMessage,
    PlatformCapabilities,
    SessionSource,
)
from personal_agent.hooks import HookEvent, HookManager, PreDeliveryOutcome


class MediaAdapter:
    capabilities = PlatformCapabilities(
        text=True,
        image_send=True,
        file_send=True,
        max_file_bytes=1024,
        max_attachments=4,
    )

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.fail_image_once = False

    async def send_message(self, chat_id, message):
        self.sent.append(("text", message.text_content()))
        return SimpleNamespace(success=True, message_id=f"text-{len(self.sent)}", error="")

    async def send_artifact(self, chat_id, *, kind, path, filename, mime_type):
        self.sent.append((kind, Path(path).read_text()))
        if self.fail_image_once:
            self.fail_image_once = False
            return SimpleNamespace(success=False, message_id="", error="temporary upload failure")
        return SimpleNamespace(success=True, message_id=f"media-{len(self.sent)}", error="")


async def _runtime(tmp_path):
    db = Database(tmp_path / "state.db")
    await db.initialize()
    artifacts = ArtifactStore(tmp_path / "artifacts", db)
    await artifacts.initialize()
    ref = await artifacts.create(
        b"image-data",
        kind="image",
        filename="result.png",
        mime_type="image/png",
        session_key="wechat:c1:u1",
        turn_id="turn-1",
    )
    sessions = SessionDirectory()
    sessions.active_key(SessionSource(platform="wechat", user_id="u1", chat_id="c1"))
    platforms = PlatformDirectory()
    adapter = MediaAdapter()
    platforms.register("wechat", adapter)
    outbox = DeliveryOutbox(db, max_attempts=3)
    service = DeliveryService(
        sessions=sessions,
        platforms=platforms,
        artifact_store=artifacts,
        outbox=outbox,
    )
    return db, ref, adapter, outbox, service


def _message(ref):
    return OutboundMessage(parts=[
        MessagePart(type="text", text="这是图片。"),
        MessagePart(
            type="image",
            artifact_id=ref.artifact_id,
            name=ref.filename,
            mime_type=ref.mime_type,
        ),
    ])


@pytest.mark.asyncio
async def test_delivery_sends_text_and_managed_image_parts(tmp_path):
    db, ref, adapter, outbox, service = await _runtime(tmp_path)
    try:
        result = await service.deliver(DeliveryRequest(
            session_key="wechat:c1:u1",
            message=_message(ref),
        ))
        assert result.status == DeliveryStatus.DELIVERED
        assert [item[0] for item in adapter.sent] == ["text", "image"]
        assert len(await outbox.parts(result.delivery_id)) == 2
        assert all(item.status == "delivered" for item in await outbox.parts(result.delivery_id))
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delivery_retry_skips_already_delivered_text_part(tmp_path):
    db, ref, adapter, outbox, service = await _runtime(tmp_path)
    adapter.fail_image_once = True
    request = DeliveryRequest(session_key="wechat:c1:u1", message=_message(ref))
    try:
        first = await service.deliver(request)
        assert first.status == DeliveryStatus.DEFERRED
        assert first.partial is True
        await db.update_delivery(request.delivery_id, next_attempt_at=0)

        results = await DeliveryWorker(service, outbox).process_due()
        assert results[0].status == DeliveryStatus.DELIVERED
        assert adapter.sent == [
            ("text", "这是图片。"),
            ("image", "image-data"),
            ("image", "image-data"),
        ]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delivery_downgrades_unsupported_artifact_without_exposing_path(tmp_path):
    db, ref, adapter, outbox, service = await _runtime(tmp_path)
    adapter.capabilities = PlatformCapabilities(text=True)
    try:
        result = await service.deliver(DeliveryRequest(
            session_key="wechat:c1:u1",
            message=_message(ref),
        ))
        assert result.delivered
        assert result.degraded is True
        assert adapter.sent[1][0] == "text"
        assert "附件未发送" in adapter.sent[1][1]
        assert str((tmp_path / "artifacts").resolve()) not in adapter.sent[1][1]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_pre_delivery_text_replacement_preserves_artifact_parts(tmp_path):
    db, ref, adapter, outbox, service = await _runtime(tmp_path)
    hooks = HookManager()
    hooks.register(
        owner="test",
        event=HookEvent.PRE_DELIVERY,
        callback=lambda event: PreDeliveryOutcome.replace_text("改写后的说明。"),
    )
    service.hook_manager = hooks
    try:
        result = await service.deliver(DeliveryRequest(
            session_key="wechat:c1:u1",
            message=_message(ref),
        ))
        assert result.delivered
        assert adapter.sent == [("text", "改写后的说明。"), ("image", "image-data")]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_pre_delivery_hook_can_remove_one_artifact(tmp_path):
    db, ref, adapter, outbox, service = await _runtime(tmp_path)
    hooks = HookManager()
    hooks.register(
        owner="test",
        event=HookEvent.PRE_DELIVERY,
        callback=lambda event: PreDeliveryOutcome.remove_artifacts(ref.artifact_id),
    )
    service.hook_manager = hooks
    try:
        result = await service.deliver(DeliveryRequest(
            session_key="wechat:c1:u1",
            message=_message(ref),
        ))
        assert result.delivered
        assert adapter.sent == [("text", "这是图片。")]
    finally:
        await db.close()
