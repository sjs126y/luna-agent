from plugins.codex_bridge.models import DevelopmentEvent, DevelopmentSession, DevelopmentStatus
from plugins.codex_bridge.prompts import summarize_event, wrap_event


def test_development_session_round_trips_persistent_fields() -> None:
    session = DevelopmentSession(
        plugin_id="user/demo",
        thread_id="thr_123",
        workspace_path="/tmp/demo",
        status=DevelopmentStatus.RUNNING.value,
        current_turn_id="turn_1",
    )

    restored = DevelopmentSession.from_dict(session.to_dict())

    assert restored.plugin_id == "user/demo"
    assert restored.thread_id == "thr_123"
    assert restored.status == "running"
    assert restored.current_turn_id == "turn_1"


def test_event_prompt_is_type_specific_and_bounded() -> None:
    event = DevelopmentEvent(
        event_id="evt_1",
        plugin_id="user/demo",
        event_type="assistant_message",
        text="Please choose a parser.",
        thread_id="thr_123",
    ).to_dict()

    prompt = wrap_event(
        plugin_id=event["plugin_id"],
        thread_id=event["thread_id"],
        event_type=event["event_type"],
        text=event["text"],
    )

    assert "assistant_message" in prompt
    assert "不要无意义地重复提问" in prompt
    assert summarize_event({**event, "text": "x" * 5000})["text"] == "x" * 1000

