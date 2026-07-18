"""Codex history provider."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from msync.models import Conversation, Event
from msync.providers.base import (
    ConversationDetails,
    HistoryFormatError,
    HistoryProvider,
    NoTransferableMessagesError,
    as_string,
    canonical_session_id,
    display_events,
    encode_jsonl,
    event_object,
    event_timestamp,
    message_parts,
)
from msync.schemas.codex import (
    CodexContentBlock,
    CodexEventMessageLine,
    CodexEventMessagePayload,
    CodexResponseMessageLine,
    CodexResponseMessagePayload,
    CodexRolloutLine,
    CodexSessionMetaLine,
    CodexSessionMetaPayload,
)

_CODEX_NON_TOOL_CALL_TYPES = frozenset({"message", "reasoning"})


def _is_tool_item(subtype: str | None) -> bool:
    """Recognize current and future Responses API tool item variants."""

    if subtype is None or subtype in _CODEX_NON_TOOL_CALL_TYPES:
        return False
    return (
        subtype.endswith(("_call", "_call_output"))
        or subtype.endswith(("_tool", "_tool_output"))
        or subtype
        in {
            "mcp_approval_request",
            "mcp_approval_response",
            "mcp_list_tools",
            "tool_search_output",
        }
    )


class CodexProvider(HistoryProvider):
    """Read Codex session and archived-session JSONL files."""

    name = "codex"
    search_directories = ("sessions", "archived_sessions")

    def matches_record(self, record: dict[str, Any]) -> bool:
        return (
            record.get("type") in {"session_meta", "response_item", "event_msg", "turn_context"}
            or "session_id" in record
        )

    def decode_event(self, sequence: int, raw_json: str, value: dict[str, Any]) -> Event:
        try:
            record = CodexRolloutLine.model_validate(value)
        except ValidationError as error:
            return Event(
                sequence=sequence,
                raw_json=raw_json,
                event_type=as_string(value.get("type")) or "invalid_record",
                parse_error=str(error),
            )

        event_type = record.type
        payload = record.payload.model_dump(mode="json") if record.payload is not None else {}
        subtype = as_string(payload.get("type"))
        role = as_string(payload.get("role"))
        visibility = "metadata"
        content: Any = None

        if event_type == "event_msg" and subtype in {"user_message", "agent_message"}:
            role = "user" if subtype == "user_message" else "assistant"
            visibility = "display"
            content = payload.get("message")
        elif event_type == "response_item" and subtype == "message":
            visibility = "model"
            content = payload.get("content")
        elif event_type == "response_item" and subtype == "reasoning":
            role = "reasoning"
            visibility = "model"
            content = payload.get("summary")
        elif event_type == "response_item" and _is_tool_item(subtype):
            role = "tool"
            visibility = "display"
            content = payload

        return Event(
            sequence=sequence,
            raw_json=raw_json,
            event_type=event_type,
            event_subtype=subtype,
            external_id=as_string(payload.get("id")),
            role=role,
            occurred_at=record.timestamp,
            visibility=visibility,
            parts=message_parts(content),
        )

    def encode_conversation(
        self,
        conversation: Conversation,
        *,
        session_id: str,
        started_at: datetime,
        source_key: str,
    ) -> bytes:
        del source_key
        events = display_events(conversation)
        if not events:
            raise NoTransferableMessagesError(
                "conversation has no displayable user or assistant messages"
            )

        utc_started_at = started_at.astimezone(UTC)
        timestamp = utc_started_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
        git = {"branch": conversation.git_branch} if conversation.git_branch else None
        records = [
            CodexSessionMetaLine(
                timestamp=timestamp,
                payload=CodexSessionMetaPayload(
                    session_id=session_id,
                    id=session_id,
                    timestamp=timestamp,
                    cwd=conversation.cwd or str(Path.home()),
                    model_provider="openai",
                    git=git,
                ),
            )
        ]
        for offset, event in enumerate(events, start=1):
            occurred_at = event_timestamp(event, utc_started_at, offset)
            text = event.searchable_text
            if event.role == "user":
                records.extend(
                    [
                        CodexResponseMessageLine(
                            timestamp=occurred_at,
                            payload=CodexResponseMessagePayload(
                                role="user",
                                content=[CodexContentBlock(type="input_text", text=text)],
                            ),
                        ),
                        CodexEventMessageLine(
                            timestamp=occurred_at,
                            payload=CodexEventMessagePayload(
                                type="user_message",
                                message=text,
                                images=[],
                                local_images=[],
                                text_elements=[],
                            ),
                        ),
                    ]
                )
            else:
                records.extend(
                    [
                        CodexEventMessageLine(
                            timestamp=occurred_at,
                            payload=CodexEventMessagePayload(
                                type="agent_message",
                                message=text,
                                phase="final_answer",
                            ),
                        ),
                        CodexResponseMessageLine(
                            timestamp=occurred_at,
                            payload=CodexResponseMessagePayload(
                                role="assistant",
                                content=[CodexContentBlock(type="output_text", text=text)],
                            ),
                        ),
                    ]
                )
        return encode_jsonl(records)

    def validate_export_schema(self, transcript: bytes) -> None:
        """Validate generated rollout envelopes and their discriminated payloads."""

        record_count = 0
        session_meta_count = 0
        for line_number, raw_line in enumerate(transcript.splitlines(), start=1):
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
                if not isinstance(value, dict):
                    raise ValueError("record is not a JSON object")
                record_type = value.get("type")
                payload = value.get("payload")
                payload_type = payload.get("type") if isinstance(payload, dict) else None
                if record_type == "session_meta":
                    CodexSessionMetaLine.model_validate(value)
                    session_meta_count += 1
                    if record_count:
                        raise ValueError("session_meta must be the first generated record")
                    if payload.get("id") != payload.get("session_id"):
                        raise ValueError("session_meta id and session_id must match")
                elif record_type == "response_item" and payload_type == "message":
                    line = CodexResponseMessageLine.model_validate(value)
                    expected_content_type = (
                        "input_text" if line.payload.role == "user" else "output_text"
                    )
                    if any(block.type != expected_content_type for block in line.payload.content):
                        raise ValueError(
                            f"{line.payload.role} messages require {expected_content_type} blocks"
                        )
                elif record_type == "event_msg" and payload_type in {
                    "user_message",
                    "agent_message",
                }:
                    line = CodexEventMessageLine.model_validate(value)
                    if line.payload.type == "user_message" and line.payload.phase is not None:
                        raise ValueError("user_message events cannot declare an assistant phase")
                else:
                    raise ValueError(
                        f"unsupported generated Codex record {record_type!r}/{payload_type!r}"
                    )
            except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as error:
                detail = " ".join(str(error).split())
                raise HistoryFormatError(
                    f"generated Codex transcript line {line_number} is invalid: {detail}"
                ) from error
            record_count += 1
        if not record_count:
            raise HistoryFormatError("generated Codex transcript contains no records")
        if session_meta_count != 1:
            raise HistoryFormatError("generated Codex transcript requires exactly one session_meta")

    def export_relative_path(
        self,
        conversation: Conversation,
        *,
        session_id: str,
        started_at: datetime,
    ) -> Path:
        del conversation
        utc = started_at.astimezone(UTC)
        date_path = Path("sessions") / f"{utc:%Y}" / f"{utc:%m}" / f"{utc:%d}"
        timestamp = utc.strftime("%Y-%m-%dT%H-%M-%S")
        return date_path / f"rollout-{timestamp}-{session_id}.jsonl"

    def conversation_details(
        self, events: tuple[Event, ...], path: Path, relative_path: str
    ) -> ConversationDetails:
        del relative_path
        metadata: dict[str, Any] = {}
        external_id: str | None = None
        logical_session_id: str | None = None
        cwd: str | None = None
        model: str | None = None
        git_branch: str | None = None

        for event in events:
            value = event_object(event)
            if value is None:
                continue
            payload = value.get("payload")
            if not isinstance(payload, dict):
                continue
            if value.get("type") == "session_meta":
                external_id = (
                    as_string(payload.get("id") or payload.get("session_id")) or external_id
                )
                cwd = as_string(payload.get("cwd")) or cwd
                provenance = payload.get("msync")
                if isinstance(provenance, dict):
                    logical_session_id = (
                        as_string(provenance.get("logical_session_id")) or logical_session_id
                    )
                    source_provider = as_string(provenance.get("source_provider"))
                    source_conversation_id = as_string(provenance.get("source_conversation_id"))
                    if (
                        logical_session_id is None
                        and source_provider is not None
                        and source_conversation_id is not None
                    ):
                        logical_session_id = canonical_session_id(
                            source_provider, source_conversation_id
                        )
                git = payload.get("git")
                if isinstance(git, dict):
                    git_branch = as_string(git.get("branch")) or git_branch
                metadata = {
                    key: payload[key]
                    for key in (
                        "cli_version",
                        "model_provider",
                        "originator",
                        "source",
                        "thread_source",
                    )
                    if key in payload
                }
                if git:
                    metadata["git"] = git
            elif value.get("type") == "turn_context":
                model = as_string(payload.get("model")) or model

        return ConversationDetails(
            external_id=external_id or path.stem,
            logical_session_id=logical_session_id,
            metadata=metadata,
            cwd=cwd,
            model=model,
            git_branch=git_branch,
        )
