from luna_agent.conversation.transcript import build_stopped_turn_transcript


def _text(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def test_stopped_transcript_keeps_completed_tool_exchange():
    messages = [
        _text("user", "do it"),
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "working"},
                {"type": "tool_use", "id": "call-1", "name": "write_file", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "ok"}],
        },
    ]

    result = build_stopped_turn_transcript(
        messages,
        current_turn_user_idx=0,
        user_text="do it",
        stop_text="已停止。",
    )

    assert [message["role"] for message in result.messages] == ["user", "assistant", "user", "assistant"]
    assert result.summary == {
        "partial": True,
        "messages_saved": 4,
        "tool_calls_saved": 1,
        "incomplete_tool_calls_dropped": 0,
    }


def test_stopped_transcript_drops_orphaned_tool_use_but_keeps_text():
    messages = [
        _text("user", "do it"),
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "starting"},
                {"type": "tool_use", "id": "call-1", "name": "write_file", "input": {}},
            ],
        },
    ]

    result = build_stopped_turn_transcript(
        messages,
        current_turn_user_idx=0,
        user_text="do it",
        stop_text="已停止。",
    )

    assert result.messages[1] == _text("assistant", "starting")
    assert result.summary["tool_calls_saved"] == 0
    assert result.summary["incomplete_tool_calls_dropped"] == 1


def test_stopped_transcript_uses_current_turn_index_after_compression():
    messages = [
        _text("user", "compressed history"),
        _text("assistant", "summary"),
        _text("user", "current request"),
    ]

    result = build_stopped_turn_transcript(
        messages,
        current_turn_user_idx=2,
        user_text="current request",
        stop_text="已停止。",
    )

    assert result.messages == [_text("user", "current request"), _text("assistant", "已停止。")]


def test_stopped_transcript_does_not_duplicate_existing_stop_marker():
    messages = [_text("user", "stop"), _text("assistant", "已停止。")]

    result = build_stopped_turn_transcript(
        messages,
        current_turn_user_idx=0,
        user_text="stop",
        stop_text="已停止。",
    )

    assert result.messages == messages
