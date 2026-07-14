"""Codex history provider."""

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
        event_type = as_string(value.get("type")) or "unknown"
        payload = value.get("payload")
        payload = payload if isinstance(payload, dict) else {}
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
            occurred_at=as_string(value.get("timestamp")),
            visibility=visibility,
            parts=message_parts(content),
        )

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
