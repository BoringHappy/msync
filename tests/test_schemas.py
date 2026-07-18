from __future__ import annotations

import pytest
from pydantic import ValidationError

from msync.schemas.claude import ClaudeRecord
from msync.schemas.codex import (
    CodexContentBlock,
    CodexResponseMessagePayload,
    CodexRolloutLine,
)


def test_claude_schema_accepts_aliases_and_retains_new_provider_fields() -> None:
    record = ClaudeRecord.model_validate(
        {
            "type": "assistant",
            "sessionId": "session-1",
            "parentUuid": "message-1",
            "isMeta": True,
            "isSidechain": True,
            "sourceToolUseID": "tool-1",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hello"}],
                "futureMessageField": True,
            },
            "futureRecordField": {"enabled": True},
        }
    )

    dumped = record.model_dump(mode="json", by_alias=True)
    assert record.session_id == "session-1"
    assert record.parent_uuid == "message-1"
    assert record.is_meta is True
    assert record.is_sidechain is True
    assert record.source_tool_use_id == "tool-1"
    assert dumped["futureRecordField"] == {"enabled": True}
    assert dumped["message"]["futureMessageField"] is True


def test_claude_schema_rejects_malformed_understood_message_fields() -> None:
    with pytest.raises(ValidationError):
        ClaudeRecord.model_validate(
            {
                "type": "user",
                "message": {"role": "user", "content": 42},
            }
        )


def test_codex_schema_serializes_typed_payload_without_losing_fields() -> None:
    line = CodexRolloutLine(
        timestamp="2026-07-14T12:00:00Z",
        type="response_item",
        payload=CodexResponseMessagePayload(
            role="assistant",
            content=[CodexContentBlock(type="output_text", text="answer")],
            future_payload_field="retained",
        ),
    )

    dumped = line.model_dump(mode="json")
    assert dumped["payload"] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "answer"}],
        "future_payload_field": "retained",
    }


def test_codex_schema_accepts_unknown_rollout_types() -> None:
    line = CodexRolloutLine.model_validate(
        {
            "timestamp": "2026-07-14T12:00:00Z",
            "type": "future_rollout_item",
            "payload": {"type": "future_payload", "newValue": 7},
        }
    )

    assert line.type == "future_rollout_item"
    assert line.payload is not None
    assert line.payload.model_dump()["newValue"] == 7
