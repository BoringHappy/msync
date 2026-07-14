"""Claude Code history provider."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from msync.models import Conversation, Event
from msync.providers.base import (
    ConversationDetails,
    HistoryProvider,
    as_string,
    canonical_session_id,
    display_events,
    encode_jsonl,
    event_object,
    event_timestamp,
    message_parts,
    stable_event_id,
)
from msync.schemas.claude import (
    ClaudeAssistantMessage,
    ClaudeAssistantRecord,
    ClaudeContentBlock,
    ClaudeRecord,
    ClaudeUsage,
    ClaudeUserMessage,
    ClaudeUserRecord,
)


class ClaudeProvider(HistoryProvider):
    """Read Claude Code project and subagent JSONL files."""

    name = "claude"
    search_directories = ("projects",)

    def matches_record(self, record: dict[str, Any]) -> bool:
        return "sessionId" in record or "uuid" in record

    def decode_event(self, sequence: int, raw_json: str, value: dict[str, Any]) -> Event:
        try:
            record = ClaudeRecord.model_validate(value)
        except ValidationError as error:
            return Event(
                sequence=sequence,
                raw_json=raw_json,
                event_type=as_string(value.get("type")) or "invalid_record",
                parse_error=str(error),
            )

        event_type = record.type
        message = record.message
        role = message.role if message is not None else None
        content: Any = None
        visibility = "metadata"

        if event_type in {"user", "assistant"}:
            role = role or event_type
            visibility = "display"
            content = message.model_dump(mode="json")["content"] if message is not None else None
        elif event_type == "system":
            role = "system"
            content = record.content
        elif event_type == "summary":
            role = "system"
            content = record.summary

        return Event(
            sequence=sequence,
            raw_json=raw_json,
            event_type=event_type,
            event_subtype=record.subtype,
            external_id=record.uuid or as_string(value.get("messageId")),
            parent_external_id=record.parent_uuid,
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

        cwd = conversation.cwd or str(Path.home())
        records: list[ClaudeRecord] = []
        parent_uuid: str | None = None
        provenance = {
            "sourceProvider": conversation.provider,
            "sourceConversationId": conversation.external_id,
            "sourceKey": source_key,
            "logicalSessionId": conversation.logical_session_id,
        }
        for offset, event in enumerate(events, start=1):
            event_id = stable_event_id(session_id, event)
            common = {
                "sessionId": session_id,
                "uuid": event_id,
                "parentUuid": parent_uuid,
                "timestamp": event_timestamp(event, started_at, offset),
                "cwd": cwd,
                "gitBranch": conversation.git_branch,
                "version": "msync-0.1.0",
                "entrypoint": "msync",
                "msync": provenance,
            }
            text = event.searchable_text
            if event.role == "user":
                records.append(
                    ClaudeUserRecord(
                        **common,
                        message=ClaudeUserMessage(content=text),
                    )
                )
            else:
                records.append(
                    ClaudeAssistantRecord(
                        **common,
                        message=ClaudeAssistantMessage(
                            id=f"msg_msync_{event_id.replace('-', '')}",
                            model=conversation.model or "imported",
                            content=[ClaudeContentBlock(type="text", text=text)],
                            usage=ClaudeUsage(),
                        ),
                    )
                )
            parent_uuid = event_id
        return encode_jsonl(records)

    def export_relative_path(
        self,
        conversation: Conversation,
        *,
        session_id: str,
        started_at: datetime,
    ) -> Path:
        del started_at
        cwd = conversation.cwd or str(Path.home())
        project = cwd.rstrip("/\\").replace("/", "-").replace("\\", "-").replace(":", "-")
        return Path("projects") / (project or "-") / f"{session_id}.jsonl"

    def conversation_details(
        self, events: tuple[Event, ...], path: Path, relative_path: str
    ) -> ConversationDetails:
        external_id: str | None = None
        logical_session_id: str | None = None
        cwd: str | None = None
        model: str | None = None
        git_branch: str | None = None
        metadata: dict[str, Any] = {}

        for event in events:
            value = event_object(event)
            if value is None:
                continue
            external_id = as_string(value.get("sessionId")) or external_id
            provenance = value.get("msync")
            if isinstance(provenance, dict):
                logical_session_id = (
                    as_string(provenance.get("logicalSessionId")) or logical_session_id
                )
                source_provider = as_string(provenance.get("sourceProvider"))
                source_conversation_id = as_string(provenance.get("sourceConversationId"))
                if (
                    logical_session_id is None
                    and source_provider is not None
                    and source_conversation_id is not None
                ):
                    logical_session_id = canonical_session_id(
                        source_provider, source_conversation_id
                    )
            cwd = as_string(value.get("cwd")) or cwd
            git_branch = as_string(value.get("gitBranch")) or git_branch
            if version := as_string(value.get("version")):
                metadata["version"] = version
            if entrypoint := as_string(value.get("entrypoint")):
                metadata["entrypoint"] = entrypoint
            message = value.get("message")
            if isinstance(message, dict):
                model = as_string(message.get("model")) or model

        is_subagent = "/subagents/" in f"/{relative_path}"
        return ConversationDetails(
            external_id=external_id or path.stem,
            logical_session_id=logical_session_id,
            metadata=metadata,
            kind="subagent" if is_subagent else "main",
            parent_external_id=path.parent.parent.name if is_subagent else None,
            cwd=cwd,
            model=model,
            git_branch=git_branch,
        )
