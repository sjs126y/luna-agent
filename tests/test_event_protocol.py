"""Conversation event protocol contract tests."""

from __future__ import annotations

import pytest

from personal_agent.conversation.events import (
    DELTA_EVENT_TYPES,
    EVENT_PROTOCOL_VERSION,
    EVENT_SCHEMAS,
    ConversationEvent,
    event_protocol_schema,
    frontend_protocol_schema,
    validate_event_contract,
)


def test_event_protocol_schema_covers_all_event_types():
    from typing import get_args

    from personal_agent.conversation.events import ConversationEventType
    from personal_agent.tui.renderer_base import Renderer

    event_types = set(get_args(ConversationEventType))
    assert set(EVENT_SCHEMAS) == event_types
    assert set(Renderer._DISPATCH) == event_types
    assert DELTA_EVENT_TYPES <= event_types


def test_event_protocol_schema_is_frontend_serializable():
    schema = event_protocol_schema()

    assert schema["protocol_version"] == EVENT_PROTOCOL_VERSION
    assert "tool_end" in schema["events"]
    assert schema["events"]["assistant_delta"]["delta"] is True
    fields = {
        field["name"]: field
        for field in schema["events"]["tool_end"]["fields"]
    }
    assert fields["tool_name"]["required"] is True
    assert fields["tool_use_id"]["required"] is True
    assert fields["full_output"]["type"] == "string"
    assert fields["display_name"]["type"] == "string"
    assert fields["available_actions"]["type"] == "list[string]"
    assert fields["input_preview"]["type"] == "string"

    decision_fields = {
        field["name"]: field
        for field in schema["events"]["tool_decision"]["fields"]
    }
    assert decision_fields["tool_name"]["required"] is True
    assert decision_fields["display_name"]["type"] == "string"
    assert decision_fields["execution_mode_label"]["type"] == "string"
    assert decision_fields["risk_summary"]["type"] == "string"
    assert decision_fields["affected_paths"]["type"] == "list[string]"
    assert decision_fields["cwd"]["type"] == "string"
    assert decision_fields["timeout_seconds"]["type"] == "number"
    assert decision_fields["method"]["type"] == "string"
    assert decision_fields["process_label"]["type"] == "string"

    retry_fields = {
        field["name"]: field
        for field in schema["events"]["retry"]["fields"]
    }
    assert retry_fields["category"]["required"] is True
    assert retry_fields["max_attempts"]["type"] == "integer"
    assert retry_fields["recoverable"]["type"] == "boolean"

    stop_fields = {
        field["name"]: field
        for field in schema["events"]["stop"]["fields"]
    }
    assert stop_fields["reason"]["type"] == "string"
    assert stop_fields["stopped_tools"]["type"] == "integer"

    error_fields = {
        field["name"]: field
        for field in schema["events"]["error"]["fields"]
    }
    assert error_fields["error"]["required"] is True
    assert error_fields["category"]["type"] == "string"
    assert error_fields["detail_id"]["type"] == "string"


def test_frontend_protocol_schema_aliases_event_protocol_schema():
    schema = event_protocol_schema()
    assert frontend_protocol_schema() == schema

    llm_fields = {
        field["name"]: field
        for field in schema["events"]["llm_end"]["fields"]
    }
    assert llm_fields["cache_hit_tokens"]["type"] == "integer"
    assert llm_fields["cache_miss_tokens"]["type"] == "integer"
    assert llm_fields["cache_write_tokens"]["type"] == "integer"
    assert llm_fields["cache_read_tokens"]["type"] == "integer"
    assert llm_fields["cache_hit_rate"]["type"] == "number"
    assert llm_fields["cache_diagnostics"]["type"] == "object"


def test_conversation_event_as_dict_includes_protocol_version():
    event = ConversationEvent(
        "tool_start",
        "调用工具 read",
        data={"tool_name": "read", "tool_use_id": "t1"},
    )

    assert event.as_dict() == {
        "protocol_version": EVENT_PROTOCOL_VERSION,
        "type": "tool_start",
        "message": "调用工具 read",
        "data": {"tool_name": "read", "tool_use_id": "t1"},
    }


@pytest.mark.parametrize(
    ("event", "expected_errors"),
    [
        (
            ConversationEvent("tool_start", data={"tool_name": "read", "tool_use_id": "t1"}),
            [],
        ),
        (
            ConversationEvent("tool_start", data={"tool_name": "read"}),
            ["tool_start.tool_use_id is required"],
        ),
        (
            ConversationEvent("assistant_delta", data={"chunk": "hi"}),
            [],
        ),
    ],
)
def test_validate_event_contract_required_fields(event, expected_errors):
    assert validate_event_contract(event) == expected_errors


def test_validate_event_contract_rejects_unknown_type():
    event = ConversationEvent("missing")  # type: ignore[arg-type]

    assert validate_event_contract(event) == ["unknown event type: missing"]
