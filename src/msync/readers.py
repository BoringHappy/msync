"""Discover and normalize Claude Code and Codex JSONL transcripts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from msync.models import Conversation, Event, MessagePart, Provider


class HistoryFormatError(ValueError):
    """Raised when a directory is not a recognizable history location."""


def detect_provider(root: Path) -> Provider:
    """Infer the history provider from its layout or a representative record."""

    name = root.name.lower()
    if name.startswith(".codex") or (root / "sessions").is_dir():
        return "codex"
    if name.startswith(".claude") or (root / "projects").is_dir():
        return "claude"

    for path in sorted(root.rglob("*.jsonl")):
        record = _first_record(path)
        if not record:
            continue
        record_type = record.get("type")
        if "sessionId" in record or "uuid" in record:
            return "claude"
        if (
            record_type in {"session_meta", "response_item", "event_msg", "turn_context"}
            or "session_id" in record
        ):
            return "codex"

    raise HistoryFormatError(
        f"Could not detect Claude or Codex history in {root}. Use --provider to specify the format."
    )


def discover_transcripts(root: Path, provider: Provider) -> list[Path]:
    """Return conversation transcripts, excluding the providers' prompt indexes."""

    if provider == "codex":
        search_roots = [
            path for path in (root / "sessions", root / "archived_sessions") if path.is_dir()
        ]
    else:
        search_roots = [root / "projects"] if (root / "projects").is_dir() else []

    if not search_roots:
        search_roots = [root]

    paths = {
        path.resolve()
        for search_root in search_roots
        for path in search_root.rglob("*.jsonl")
        if path.is_file() and path.name != "history.jsonl"
    }
    return sorted(paths)


def read_conversation(
    path: Path,
    root: Path,
    provider: Provider,
    *,
    transcript: bytes | None = None,
) -> Conversation:
    """Read one JSONL file while retaining its exact original bytes."""

    transcript = path.read_bytes() if transcript is None else transcript
    raw_events = tuple(_decode_events(transcript, provider))
    relative_path = path.relative_to(root).as_posix()
    if provider == "codex":
        details = _codex_details(raw_events, path)
    else:
        details = _claude_details(raw_events, path, relative_path)

    timestamps = [event.occurred_at for event in raw_events if event.occurred_at]
    title = _first_user_title(raw_events)
    return Conversation(
        path=path,
        relative_path=relative_path,
        provider=provider,
        transcript=transcript,
        sha256=hashlib.sha256(transcript).hexdigest(),
        events=raw_events,
        title=title,
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
        **details,
    )


def _first_record(path: Path) -> dict[str, Any] | None:
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


def _decode_events(transcript: bytes, provider: Provider) -> Iterable[Event]:
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

        if provider == "codex":
            yield _decode_codex_event(sequence, raw_json, value)
        else:
            yield _decode_claude_event(sequence, raw_json, value)


def _decode_codex_event(sequence: int, raw_json: str, value: dict[str, Any]) -> Event:
    event_type = _as_string(value.get("type")) or "unknown"
    payload = value.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    subtype = _as_string(payload.get("type"))
    role = _as_string(payload.get("role"))
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
        external_id=_as_string(payload.get("id")),
        role=role,
        occurred_at=_as_string(value.get("timestamp")),
        visibility=visibility,
        parts=_message_parts(content),
    )


def _decode_claude_event(sequence: int, raw_json: str, value: dict[str, Any]) -> Event:
    event_type = _as_string(value.get("type")) or "unknown"
    message = value.get("message")
    message = message if isinstance(message, dict) else {}
    role = _as_string(message.get("role"))
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
        event_subtype=_as_string(value.get("subtype")),
        external_id=_as_string(value.get("uuid") or value.get("messageId")),
        parent_external_id=_as_string(value.get("parentUuid")),
        role=role,
        occurred_at=_as_string(value.get("timestamp")),
        visibility=visibility,
        parts=_message_parts(content),
    )


def _message_parts(content: Any) -> tuple[MessagePart, ...]:
    if content is None:
        return ()
    blocks = content if isinstance(content, list) else [content]
    parts: list[MessagePart] = []
    for sequence, block in enumerate(blocks):
        if isinstance(block, dict):
            content_type = _as_string(block.get("type")) or "object"
        else:
            content_type = "text" if isinstance(block, str) else type(block).__name__
        parts.append(
            MessagePart(
                sequence=sequence,
                content_type=content_type,
                text=_block_text(block),
                raw_json=json.dumps(block, ensure_ascii=False, separators=(",", ":")),
            )
        )
    return tuple(parts)


def _block_text(block: Any) -> str | None:
    if isinstance(block, str):
        return block
    if isinstance(block, list):
        text = [item for value in block if (item := _block_text(value))]
        return "\n".join(text) or None
    if not isinstance(block, dict):
        return None
    for key in ("text", "input_text", "output_text", "content", "message"):
        if key in block and (text := _block_text(block[key])):
            return text
    return None


def _codex_details(events: tuple[Event, ...], path: Path) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    external_id: str | None = None
    cwd: str | None = None
    model: str | None = None
    git_branch: str | None = None

    for event in events:
        try:
            value = json.loads(event.raw_json)
        except json.JSONDecodeError:
            continue
        payload = value.get("payload")
        if not isinstance(payload, dict):
            continue
        if value.get("type") == "session_meta":
            external_id = _as_string(payload.get("id") or payload.get("session_id")) or external_id
            cwd = _as_string(payload.get("cwd")) or cwd
            git = payload.get("git")
            if isinstance(git, dict):
                git_branch = _as_string(git.get("branch")) or git_branch
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
            model = _as_string(payload.get("model")) or model

    return {
        "external_id": external_id or path.stem,
        "metadata": metadata,
        "cwd": cwd,
        "model": model,
        "git_branch": git_branch,
    }


def _claude_details(events: tuple[Event, ...], path: Path, relative_path: str) -> dict[str, Any]:
    external_id: str | None = None
    cwd: str | None = None
    model: str | None = None
    git_branch: str | None = None
    metadata: dict[str, Any] = {}

    for event in events:
        try:
            value = json.loads(event.raw_json)
        except json.JSONDecodeError:
            continue
        external_id = _as_string(value.get("sessionId")) or external_id
        cwd = _as_string(value.get("cwd")) or cwd
        git_branch = _as_string(value.get("gitBranch")) or git_branch
        if version := _as_string(value.get("version")):
            metadata["version"] = version
        if entrypoint := _as_string(value.get("entrypoint")):
            metadata["entrypoint"] = entrypoint
        message = value.get("message")
        if isinstance(message, dict):
            model = _as_string(message.get("model")) or model

    is_subagent = "/subagents/" in f"/{relative_path}"
    parent_external_id = path.parent.parent.name if is_subagent else None
    return {
        "external_id": external_id or path.stem,
        "metadata": metadata,
        "kind": "subagent" if is_subagent else "main",
        "parent_external_id": parent_external_id,
        "cwd": cwd,
        "model": model,
        "git_branch": git_branch,
    }


def _first_user_title(events: tuple[Event, ...]) -> str | None:
    for event in events:
        if event.role != "user" or event.visibility != "display":
            continue
        title = " ".join(event.searchable_text.split())
        if title:
            return title[:160]
    return None


def _as_string(value: Any) -> str | None:
    return cast(str, value) if isinstance(value, str) and value else None
