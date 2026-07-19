from luna_agent.conversation import SessionDirectory
from luna_agent.models.messages import SessionSource
from luna_agent.models.session import SessionEntry


def _source(chat_id: str = "c1") -> SessionSource:
    return SessionSource(platform="telegram", chat_id=chat_id, user_id="u1")


def test_session_directory_resolves_latest_platform_binding():
    directory = SessionDirectory()
    source = _source()

    key = directory.active_key(source)
    binding = directory.resolve(key)

    assert key == "telegram:c1:u1"
    assert binding.source.chat_id == "c1"
    source.chat_id = "mutated"
    assert binding.source.chat_id == "c1"


def test_named_session_keeps_delivery_binding_through_rename():
    directory = SessionDirectory()
    source = _source()

    old_key = directory.switch(source, "work")
    new_key = "telegram:renamed:u1"
    directory.rename(source, old_key, new_key)

    assert directory.active_key(source) == new_key
    assert directory.resolve(new_key).source.chat_id == "c1"
    assert directory.resolve(old_key) is None


def test_delete_restores_base_session_binding():
    directory = SessionDirectory()
    source = _source()
    target = directory.switch(source, "work")

    base = directory.delete(source, target)

    assert directory.active_key(source) == base
    assert directory.resolve(base).source.user_id == "u1"


def test_restore_rehydrates_delivery_binding_from_persisted_session():
    directory = SessionDirectory()

    restored = directory.restore((SessionEntry(
        session_id="session-id",
        session_key="wechat:work:u1",
        platform="wechat",
        user_id="u1",
        user_name="User",
        chat_id="chat-1",
        chat_type="group",
    ),))

    binding = directory.resolve("wechat:work:u1")
    assert restored == 1
    assert binding is not None
    assert binding.source.platform == "wechat"
    assert binding.source.chat_id == "chat-1"
    assert binding.source.chat_type == "group"
