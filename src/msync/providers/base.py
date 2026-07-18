"""Shared contracts and JSONL machinery for history providers."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid5

from msync.models import Conversation, Event, MessagePart
from msync.schemas.base import NativeRecord


class HistoryFormatError(ValueError):
    """Raised when a history provider cannot be identified or loaded."""


class NoTransferableMessagesError(ValueError):
    """Raised when a conversation cannot produce a useful native transcript."""


@dataclass(slots=True)
class ConversationDetails:
    """Provider-specific session fields normalized into the common schema."""

    external_id: str
    logical_session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    kind: str = "main"
    parent_external_id: str | None = None
    cwd: str | None = None
    model: str | None = None
    git_branch: str | None = None


class HistoryProvider(ABC):
    """Extension point implemented independently by each history source."""

    name: str
    search_directories: tuple[str, ...]
    ignored_filenames: frozenset[str] = frozenset({"history.jsonl"})

    @abstractmethod
    def matches_record(self, record: dict[str, Any]) -> bool:
        """Return whether a representative JSONL record belongs to this provider."""

    @abstractmethod
    def decode_event(self, sequence: int, raw_json: str, value: dict[str, Any]) -> Event:
        """Normalize one valid JSON object without discarding its source representation."""

    @abstractmethod
    def conversation_details(
        self, events: tuple[Event, ...], path: Path, relative_path: str
    ) -> ConversationDetails:
        """Extract provider-specific conversation metadata."""

    def discover(self, root: Path) -> list[Path]:
        """Find transcript files in this provider's standard subdirectories."""

        search_roots = [
            root / directory for directory in self.search_directories if (root / directory).is_dir()
        ]
        if not search_roots:
            search_roots = [root]
        paths = {
            path.resolve()
            for search_root in search_roots
            for path in search_root.rglob("*.jsonl")
            if path.is_file() and path.name not in self.ignored_filenames
        }
        return sorted(paths)

    def matches_name(self, root: Path) -> bool:
        """Match the provider name anywhere in the directory basename."""

        return self.name.casefold() in root.name.casefold()

    def detection_paths(self, root: Path) -> list[Path]:
        """Return only fixed provider locations used for content detection."""

        paths: set[Path] = set()
        history = root / "history.jsonl"
        if history.is_file():
            paths.add(history.resolve())
        for directory in self.search_directories:
            search_root = root / directory
            if search_root.is_dir():
                paths.update(
                    path.resolve() for path in search_root.rglob("*.jsonl") if path.is_file()
                )
        return sorted(paths)

    def encode_conversation(
        self,
        conversation: Conversation,
        *,
        session_id: str,
        started_at: datetime,
        source_key: str,
    ) -> bytes:
        """Convert a normalized conversation into this provider's native JSONL."""

        del conversation, session_id, started_at, source_key
        raise HistoryFormatError(f"Provider {self.name!r} does not support transcript export.")

    def export_relative_path(
        self,
        conversation: Conversation,
        *,
        session_id: str,
        started_at: datetime,
    ) -> Path:
        """Return the canonical native location for a converted conversation."""

        del conversation, session_id, started_at
        raise HistoryFormatError(f"Provider {self.name!r} does not support transcript export.")

    def validate_export_schema(self, transcript: bytes) -> None:
        """Validate the strict record subset emitted by this provider."""

        del transcript

    def read(
        self,
        path: Path,
        root: Path,
        *,
        transcript: bytes | None = None,
        logical_session_id: str | None = None,
    ) -> Conversation:
        """Read one transcript while retaining its exact source bytes."""

        transcript = path.read_bytes() if transcript is None else transcript
        events = tuple(self._decode_events(transcript))
        relative_path = path.relative_to(root).as_posix()
        details = self.conversation_details(events, path, relative_path)
        logical_session_id = normalized_session_id(
            logical_session_id or details.logical_session_id,
            provider=self.name,
            external_id=details.external_id,
        )
        timestamps = [event.occurred_at for event in events if event.occurred_at]
        return Conversation(
            path=path,
            relative_path=relative_path,
            provider=self.name,
            transcript=transcript,
            sha256=hashlib.sha256(transcript).hexdigest(),
            chat_sha256=canonical_chat_sha256(events),
            external_id=details.external_id,
            logical_session_id=logical_session_id,
            events=events,
            metadata=details.metadata,
            kind=details.kind,
            parent_external_id=details.parent_external_id,
            title=first_user_title(events),
            cwd=details.cwd,
            model=details.model,
            git_branch=details.git_branch,
            started_at=min(timestamps) if timestamps else None,
            ended_at=max(timestamps) if timestamps else None,
        )

    def validate_transcript(
        self,
        path: Path,
        root: Path,
        *,
        transcript: bytes,
        logical_session_id: str | None = None,
        strict_export: bool = False,
    ) -> Conversation:
        """Fail closed when candidate bytes cannot be read as a native transcript."""

        if strict_export:
            self.validate_export_schema(transcript)
        conversation = self.read(
            path,
            root,
            transcript=transcript,
            logical_session_id=logical_session_id,
        )
        if not conversation.events:
            raise HistoryFormatError("generated transcript contains no JSON records")
        invalid_events = [event for event in conversation.events if event.parse_error]
        if invalid_events:
            first = invalid_events[0]
            detail = " ".join((first.parse_error or "invalid record").split())
            raise HistoryFormatError(
                f"generated transcript line {first.sequence + 1} is invalid: {detail}"
            )
        return conversation

    def _decode_events(self, transcript: bytes) -> Iterable[Event]:
        for sequence, raw_line in enumerate(transcript.splitlines()):
            if not raw_line.strip():
                continue
            raw_json = raw_line.decode("utf-8", errors="replace")
            try:
                value = json.loads(raw_json)
                if not isinstance(value, dict):
                    raise ValueError("JSON record is not an object")
            except (json.JSONDecodeError, ValueError) as error:
                yield Event(
                    sequence=sequence,
                    raw_json=raw_json,
                    event_type="invalid_json",
                    parse_error=str(error),
                )
                continue
            yield self.decode_event(sequence, raw_json, value)


def first_json_record(path: Path) -> dict[str, Any] | None:
    """Read only enough of a transcript to identify its provider."""

    try:
        with path.open("rb") as stream:
            for line in stream:
                if not line.strip():
                    continue
                value = json.loads(line)
                return value if isinstance(value, dict) else None
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return None
    return None


def message_parts(content: Any) -> tuple[MessagePart, ...]:
    """Normalize string or structured message content into ordered blocks."""

    if content is None:
        return ()
    blocks = content if isinstance(content, list) else [content]
    parts: list[MessagePart] = []
    for sequence, block in enumerate(blocks):
        if isinstance(block, dict):
            content_type = as_string(block.get("type")) or "object"
        else:
            content_type = "text" if isinstance(block, str) else type(block).__name__
        parts.append(
            MessagePart(
                sequence=sequence,
                content_type=content_type,
                text=block_text(block),
                raw_json=json.dumps(block, ensure_ascii=False, separators=(",", ":")),
            )
        )
    return tuple(parts)


def block_text(block: Any) -> str | None:
    """Extract human-readable text from a provider message block."""

    if isinstance(block, str):
        return block
    if isinstance(block, list):
        text = [item for value in block if (item := block_text(value))]
        return "\n".join(text) or None
    if not isinstance(block, dict):
        return None
    for key in (
        "text",
        "input_text",
        "output_text",
        "content",
        "message",
        "arguments",
        "output",
        "result",
    ):
        if key in block and (text := block_text(block[key])):
            return text
    return None


def event_object(event: Event) -> dict[str, Any] | None:
    """Decode a retained event when extracting conversation-level metadata."""

    try:
        value = json.loads(event.raw_json)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def first_user_title(events: tuple[Event, ...]) -> str | None:
    for event in events:
        if event.role != "user" or event.visibility != "display":
            continue
        title = " ".join(event.searchable_text.split())
        if title:
            return title[:160]
    return None


def as_string(value: Any) -> str | None:
    return cast(str, value) if isinstance(value, str) and value else None


def display_events(conversation: Conversation) -> tuple[Event, ...]:
    """Return human-visible user/assistant turns that can cross provider boundaries."""

    return transferable_events(conversation.events)


def transferable_events(events: Iterable[Event]) -> tuple[Event, ...]:
    """Return provider-neutral turns without double-counting mirrored model events."""

    candidates = tuple(
        event
        for event in events
        if event.visibility in {"display", "model"}
        and event.role in {"user", "assistant"}
        and event.searchable_text.strip()
    )
    display_counts = Counter(
        (event.role, event.searchable_text) for event in candidates if event.visibility == "display"
    )
    selected: list[Event] = []
    for event in candidates:
        identity = (event.role, event.searchable_text)
        if event.visibility == "model" and display_counts[identity]:
            display_counts[identity] -= 1
            continue
        selected.append(event)
    return tuple(selected)


def canonical_chat_sha256(events: Iterable[Event]) -> str | None:
    """Hash provider-independent visible turns while preserving order and boundaries."""

    messages = [[event.role, event.searchable_text] for event in transferable_events(events)]
    if not messages:
        return None
    canonical = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def canonical_session_id(provider: str, external_id: str) -> str:
    """Return a stable UUID for one logical session across locations and formats."""

    try:
        return str(UUID(external_id))
    except ValueError:
        return str(uuid5(UUID("2561dd24-f9ac-4e7a-b4ca-4f72b1f5d907"), f"{provider}:{external_id}"))


def normalized_session_id(
    supplied: str | None,
    *,
    provider: str,
    external_id: str,
) -> str:
    """Validate an inherited logical UUID or derive one from the native session identity."""

    if supplied is not None:
        try:
            return str(UUID(supplied))
        except ValueError:
            pass
    return canonical_session_id(provider, external_id)


def stable_event_id(session_id: str, event: Event) -> str:
    """Derive an idempotent UUID for a converted native message."""

    return str(uuid5(UUID(session_id), f"{event.sequence}:{event.role}:{event.searchable_text}"))


def event_timestamp(event: Event, started_at: datetime, offset: int) -> str:
    """Return an RFC 3339 timestamp, falling back to stable sequence ordering."""

    if event.occurred_at:
        try:
            parsed = datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            return _rfc3339(parsed)
    return _rfc3339(started_at + timedelta(microseconds=offset))


def encode_jsonl(records: Iterable[NativeRecord]) -> bytes:
    """Serialize validated native records as newline-terminated UTF-8 JSONL."""

    return (
        "".join(
            record.model_dump_json(by_alias=True, exclude_none=True) + "\n" for record in records
        )
    ).encode()


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
