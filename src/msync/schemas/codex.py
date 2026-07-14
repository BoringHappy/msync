"""Pydantic models for Codex rollout JSONL records."""

from __future__ import annotations

from typing import Literal

from pydantic import SerializeAsAny

from msync.schemas.base import NativeRecord


class CodexPayload(NativeRecord):
    """Common payload shape for an evolving Codex rollout item."""

    type: str | None = None


class CodexContentBlock(NativeRecord):
    """One model-visible Codex input or output text block."""

    type: Literal["input_text", "output_text"]
    text: str


class CodexSyncProvenance(NativeRecord):
    """Stable msync identity retained across provider conversions."""

    source_provider: str | None = None
    source_conversation_id: str | None = None
    source_key: str | None = None
    logical_session_id: str | None = None


class CodexSessionMetaPayload(CodexPayload):
    """Metadata required for Codex to discover and resume a rollout."""

    session_id: str
    id: str
    timestamp: str
    cwd: str
    originator: str = "msync"
    cli_version: str = "msync"
    source: str = "cli"
    model_provider: str | None = None
    base_instructions: dict[str, str] | None = None
    history_mode: str = "legacy"
    git: dict[str, str] | None = None
    msync: CodexSyncProvenance | None = None


class CodexResponseMessagePayload(CodexPayload):
    """A model-visible user or assistant response item."""

    type: Literal["message"] = "message"
    role: Literal["user", "assistant"]
    content: list[CodexContentBlock]


class CodexEventMessagePayload(CodexPayload):
    """A user-facing message event replayed by Codex's session UI."""

    type: Literal["user_message", "agent_message"]
    message: str
    phase: str | None = None
    images: list[str] | None = None
    local_images: list[str] | None = None
    text_elements: list[dict[str, object]] | None = None


class CodexRolloutLine(NativeRecord):
    """Common timestamped envelope for every Codex rollout item."""

    timestamp: str | None = None
    ordinal: int | None = None
    type: str
    payload: SerializeAsAny[CodexPayload] | None = None
