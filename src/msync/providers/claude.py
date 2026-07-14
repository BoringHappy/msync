"""Claude Code history provider."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from msync.models import Event
from msync.providers.base import (
    ConversationDetails,
    HistoryProvider,
    as_string,
    event_object,
    message_parts,
)


class ClaudeProvider(HistoryProvider):
    """Read Claude Code project and subagent JSONL files."""

    name = "claude"
    search_directories = ("projects",)

    def matches_record(self, record: dict[str, Any]) -> bool:
        return "sessionId" in record or "uuid" in record

    def decode_event(self, sequence: int, raw_json: str, value: dict[str, Any]) -> Event:
        event_type = as_string(value.get("type")) or "unknown"
        message = value.get("message")
        message = message if isinstance(message, dict) else {}
        role = as_string(message.get("role"))
        content: Any = None
        visibility = "metadata"

        if event_type in {"user", "assistant"}:
            role = role or event_type
            visibility = "display"
            content = message.get("content")
        elif event_type == "system":
            role = "system"
            content = value.get("content")
        elif event_type == "summary":
            role = "system"
            content = value.get("summary")

        return Event(
            sequence=sequence,
            raw_json=raw_json,
            event_type=event_type,
            event_subtype=as_string(value.get("subtype")),
            external_id=as_string(value.get("uuid") or value.get("messageId")),
            parent_external_id=as_string(value.get("parentUuid")),
            role=role,
            occurred_at=as_string(value.get("timestamp")),
            visibility=visibility,
            parts=message_parts(content),
        )

    def conversation_details(
        self, events: tuple[Event, ...], path: Path, relative_path: str
    ) -> ConversationDetails:
        external_id: str | None = None
        cwd: str | None = None
        model: str | None = None
        git_branch: str | None = None
        metadata: dict[str, Any] = {}

        for event in events:
            value = event_object(event)
            if value is None:
                continue
            external_id = as_string(value.get("sessionId")) or external_id
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
            metadata=metadata,
            kind="subagent" if is_subagent else "main",
            parent_external_id=path.parent.parent.name if is_subagent else None,
            cwd=cwd,
            model=model,
            git_branch=git_branch,
        )
