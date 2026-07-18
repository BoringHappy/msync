from __future__ import annotations

import pytest
from pydantic import ValidationError

from msync.schemas.claude import (
    ClaudeGeneratedTranscript,
    ClaudeRecord,
    ClaudeUserMessage,
    ClaudeUserRecord,
)
from msync.schemas.codex import (
    CodexContentBlock,
    CodexEventMessagePayload,
    CodexGeneratedTranscript,
    CodexResponseMessageLine,
    CodexResponseMessagePayload,
    CodexRolloutLine,
    CodexSessionMetaPayload,
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


def test_codex_writer_schema_serializes_only_declared_payload_fields() -> None:
    line = CodexResponseMessageLine(
        timestamp="2026-07-14T12:00:00Z",
        payload=CodexResponseMessagePayload(
            role="assistant",
            content=[CodexContentBlock(type="output_text", text="answer")],
        ),
    )

    dumped = line.model_dump(mode="json")
    assert dumped["payload"] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "answer"}],
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CodexResponseMessagePayload(
            role="assistant",
            content=[CodexContentBlock(type="output_text", text="answer")],
            future_payload_field="rejected",
        )


def test_codex_writer_schema_allows_distinct_session_and_thread_ids() -> None:
    payload = CodexSessionMetaPayload(
        session_id="019f61a0-0000-7000-8000-000000000001",
        id="019f61a0-0000-7000-8000-000000000002",
        timestamp="2026-07-14T12:00:00Z",
        cwd="/work",
    )

    assert payload.session_id != payload.id


def test_codex_writer_schema_requires_role_appropriate_content_blocks() -> None:
    with pytest.raises(ValidationError, match="assistant messages require output_text blocks"):
        CodexResponseMessagePayload(
            role="assistant",
            content=[CodexContentBlock(type="input_text", text="answer")],
        )


def test_codex_writer_schema_rejects_assistant_phase_on_user_events() -> None:
    with pytest.raises(
        ValidationError,
        match="user_message events cannot declare an assistant phase",
    ):
        CodexEventMessagePayload(
            type="user_message",
            message="question",
            phase="final_answer",
        )


def test_codex_transcript_schema_requires_session_metadata_first() -> None:
    with pytest.raises(ValidationError, match="session_meta must be the first generated record"):
        CodexGeneratedTranscript.model_validate(
            [
                {
                    "timestamp": "2026-07-14T12:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "question"}],
                    },
                },
                {
                    "timestamp": "2026-07-14T12:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "session_id": "019f61a0-0000-7000-8000-000000000001",
                        "id": "019f61a0-0000-7000-8000-000000000001",
                        "timestamp": "2026-07-14T12:00:00Z",
                        "cwd": "/work",
                    },
                },
            ]
        )


def test_claude_transcript_schema_requires_unique_contiguous_message_ids() -> None:
    record = {
        "type": "user",
        "sessionId": "019f61a0-0000-7000-8000-000000000001",
        "uuid": "019f61a0-0000-7000-8000-000000000002",
        "parentUuid": None,
        "timestamp": "2026-07-14T12:00:00Z",
        "cwd": "/work",
        "message": {"role": "user", "content": "question"},
    }
    duplicate = {**record, "parentUuid": record["uuid"]}

    with pytest.raises(ValidationError, match="generated record UUIDs must be unique"):
        ClaudeGeneratedTranscript.model_validate([record, duplicate])


def test_claude_writer_schema_rejects_unknown_record_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ClaudeUserRecord(
            sessionId="019f61a0-0000-7000-8000-000000000001",
            uuid="019f61a0-0000-7000-8000-000000000002",
            timestamp="2026-07-14T12:00:00Z",
            cwd="/work",
            message=ClaudeUserMessage(content="hello"),
            futureRecordField=True,
        )


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
