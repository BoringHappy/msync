"""Data passed between provider adapters and the portable archive."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

type Provider = str


@dataclass(slots=True, frozen=True)
class MessagePart:
    """A searchable content block extracted from an event."""

    sequence: int
    content_type: str
    text: str | None
    raw_json: str


@dataclass(slots=True, frozen=True)
class Event:
    """One source JSONL record plus its normalized message content."""

    sequence: int
    raw_json: str
    event_type: str
    event_subtype: str | None = None
    external_id: str | None = None
    parent_external_id: str | None = None
    role: str | None = None
    occurred_at: str | None = None
    visibility: str = "metadata"
    parse_error: str | None = None
    parts: tuple[MessagePart, ...] = ()

    @property
    def searchable_text(self) -> str:
        return "\n".join(part.text for part in self.parts if part.text)


@dataclass(slots=True, frozen=True)
class Conversation:
    """A complete transcript and its normalized index records."""

    path: Path
    relative_path: str
    provider: Provider
    transcript: bytes
    sha256: str
    chat_sha256: str | None
    external_id: str
    logical_session_id: str
    events: tuple[Event, ...]
    metadata: dict[str, Any] = field(default_factory=dict)
    kind: str = "main"
    parent_external_id: str | None = None
    title: str | None = None
    cwd: str | None = None
    model: str | None = None
    git_branch: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
