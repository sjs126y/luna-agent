from __future__ import annotations

import pytest

from personal_agent.attachments.store import AttachmentStore, AttachmentStoreError
from personal_agent.models.messages import AttachmentRef
from personal_agent.tools.sandbox import init_sandbox


def test_attachment_store_caches_local_file_by_hash(tmp_path):
    source = tmp_path / "photo.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\npayload")
    init_sandbox([tmp_path], [])
    store = AttachmentStore(tmp_path / "cache")

    first = store.resolve(AttachmentRef(id="a1", kind="image", name="photo.png", local_path=str(source)))
    second = store.resolve(AttachmentRef(id="a1", kind="image", name="photo.png", local_path=str(source)))

    assert first.sha256 == second.sha256
    assert first.local_path == second.local_path
    assert first.mime_type == "image/png"


def test_attachment_store_blocks_path_outside_sandbox(tmp_path):
    allowed = tmp_path / "allowed"
    blocked = tmp_path / "blocked"
    allowed.mkdir()
    blocked.mkdir()
    source = blocked / "secret.txt"
    source.write_text("secret", encoding="utf-8")
    init_sandbox([allowed], [])
    store = AttachmentStore(tmp_path / "cache")

    with pytest.raises(AttachmentStoreError) as exc:
        store.resolve(AttachmentRef(id="f1", kind="file", name="secret.txt", local_path=str(source)))

    assert exc.value.reason == "path_not_allowed"


def test_attachment_store_blocks_oversized_file(tmp_path):
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * 12)
    init_sandbox([tmp_path], [])
    store = AttachmentStore(tmp_path / "cache", max_bytes_by_kind={"file": 10})

    with pytest.raises(AttachmentStoreError) as exc:
        store.resolve(AttachmentRef(id="f1", kind="file", name="big.bin", local_path=str(source)))

    assert exc.value.reason == "size_exceeded"
