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

_CLAUDE_TEXT_BLOCK_TYPES = frozenset({"text"})
_CLAUDE_REASONING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})


def _is_tool_block(content_type: str) -> bool:
    """Recognize client and server tool blocks without coupling to individual tools."""

    return content_type.endswith(("tool_use", "tool_result"))


def _injected_context_subtype(record: ClaudeRecord, text: str) -> str | None:
    """Classify Claude's synthetic user records without hiding genuine prompts."""

    stripped = text.lstrip()
    if stripped.startswith("Base directory for this skill:"):
        marker_subtype = "skill_context"
    elif stripped.startswith("[SYSTEM NOTIFICATION - NOT USER INPUT]"):
        marker_subtype = "system_notification"
    else:
        marker_subtype = "injected_context"

    if record.is_meta is True:
        return marker_subtype
    if (record.is_sidechain is True or record.source_tool_use_id) and marker_subtype != (
        "injected_context"
    ):
        return marker_subtype
    return None


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

        parts = message_parts(content)
        normalized_text = None
        event_subtype = record.subtype
        if event_type in {"user", "assistant"}:
            block_types = {part.content_type for part in parts}
            text = "\n".join(
                part.text
                for part in parts
                if part.text and part.content_type in _CLAUDE_TEXT_BLOCK_TYPES
            )
            if text:
                # Claude can put prose and tool blocks in one native message. Keep the
                # tool blocks as parts, but do not fold their payload into the prose.
                normalized_text = text
                injected_subtype = (
                    _injected_context_subtype(record, text)
                    if event_type == "user"
                    and block_types
                    and block_types <= _CLAUDE_TEXT_BLOCK_TYPES
                    else None
                )
                if injected_subtype is not None:
                    # Skill expansions and notifications are model context, not human
                    # turns. Keep their parts and raw JSON for lossless inspection while
                    # removing the potentially huge payload from human-message indexes.
                    role = "metadata"
                    visibility = "metadata"
                    event_subtype = record.subtype or injected_subtype
                    normalized_text = ""
            elif any(_is_tool_block(content_type) for content_type in block_types):
                role = "tool"
                visibility = "display"
                event_type_detail = next(iter(block_types)) if len(block_types) == 1 else "tools"
                event_subtype = record.subtype or event_type_detail
                normalized_text = "\n".join(part.text for part in parts if part.text)
            elif block_types and block_types <= _CLAUDE_REASONING_BLOCK_TYPES:
                role = "reasoning"
                visibility = "model"
                event_type_detail = (
                    next(iter(block_types)) if len(block_types) == 1 else "reasoning"
                )
                event_subtype = record.subtype or event_type_detail
                normalized_text = "\n".join(part.text for part in parts if part.text)

        return Event(
            sequence=sequence,
            raw_json=raw_json,
            event_type=event_type,
            event_subtype=event_subtype,
            external_id=record.uuid or as_string(value.get("messageId")),
            parent_external_id=record.parent_uuid,
            role=role,
            occurred_at=record.timestamp,
            visibility=visibility,
            parts=parts,
            normalized_text=normalized_text,
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
