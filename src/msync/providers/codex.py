"""Codex history provider."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from msync.models import Conversation, Event
from msync.providers.base import (
    ConversationDetails,
    HistoryProvider,
    as_string,
    display_events,
    encode_jsonl,
    event_object,
    event_timestamp,
    message_parts,
)
from msync.schemas.codex import (
    CodexContentBlock,
    CodexEventMessagePayload,
    CodexResponseMessagePayload,
    CodexRolloutLine,
    CodexSessionMetaPayload,
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
            visibility = "model"
            content = payload.get("summary")

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
        events = display_events(conversation)
        if not events:
            raise ValueError("conversation has no displayable user or assistant messages")

        utc_started_at = started_at.astimezone(UTC)
        timestamp = utc_started_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
        git = {"branch": conversation.git_branch} if conversation.git_branch else None
        records: list[CodexRolloutLine] = [
            CodexRolloutLine(
                timestamp=timestamp,
                type="session_meta",
                payload=CodexSessionMetaPayload(
                    session_id=session_id,
                    id=session_id,
                    timestamp=timestamp,
                    cwd=conversation.cwd or str(Path.home()),
                    model_provider="openai",
                    git=git,
                    msync={
                        "source_provider": conversation.provider,
                        "source_conversation_id": conversation.external_id,
                        "source_key": source_key,
                    },
                ),
            )
        ]
        for offset, event in enumerate(events, start=1):
            occurred_at = event_timestamp(event, utc_started_at, offset)
            text = event.searchable_text
            if event.role == "user":
                records.extend(
                    [
                        CodexRolloutLine(
                            timestamp=occurred_at,
                            type="response_item",
                            payload=CodexResponseMessagePayload(
                                role="user",
                                content=[CodexContentBlock(type="input_text", text=text)],
                            ),
                        ),
                        CodexRolloutLine(
                            timestamp=occurred_at,
                            type="event_msg",
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
                        CodexRolloutLine(
                            timestamp=occurred_at,
                            type="event_msg",
                            payload=CodexEventMessagePayload(
                                type="agent_message",
                                message=text,
                                phase="final_answer",
                            ),
                        ),
                        CodexRolloutLine(
                            timestamp=occurred_at,
                            type="response_item",
                            payload=CodexResponseMessagePayload(
                                role="assistant",
                                content=[CodexContentBlock(type="output_text", text=text)],
                            ),
                        ),
                    ]
                )
        return encode_jsonl(records)

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
            metadata=metadata,
            cwd=cwd,
            model=model,
            git_branch=git_branch,
        )
