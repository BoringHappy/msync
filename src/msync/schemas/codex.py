"""Pydantic models for Codex rollout JSONL records."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, SerializeAsAny

from msync.schemas.base import NativeRecord, StrictNativeRecord


class CodexPayload(NativeRecord):
    """Common payload shape for an evolving Codex rollout item."""

    type: str | None = None


class CodexContentBlock(StrictNativeRecord):
    """One model-visible Codex input or output text block."""

    type: Literal["input_text", "output_text"]
    text: str


class CodexSyncProvenance(NativeRecord):
    """Stable msync identity retained across provider conversions."""

    source_provider: str | None = None
    source_conversation_id: str | None = None
    source_key: str | None = None
    logical_session_id: str | None = None


class CodexSessionMetaPayload(StrictNativeRecord):
    """Metadata required for Codex to discover and resume a rollout."""

    session_id: UUID
    id: UUID
    timestamp: str
    cwd: str
    type: None = None
    originator: Literal["msync"] = "msync"
    cli_version: str = Field(default="0.1.0", pattern=r"^\d+\.\d+\.\d+(?:[-+].+)?$")
    source: Literal["cli"] = "cli"
    model_provider: Literal["openai"] | None = None
    base_instructions: dict[str, str] | None = None
    history_mode: Literal["legacy"] = "legacy"
    git: dict[str, str] | None = None


class CodexResponseMessagePayload(StrictNativeRecord):
    """A model-visible user or assistant response item."""

    type: Literal["message"] = "message"
    role: Literal["user", "assistant"]
    content: list[CodexContentBlock]


class CodexEventMessagePayload(StrictNativeRecord):
    """A user-facing message event replayed by Codex's session UI."""

    type: Literal["user_message", "agent_message"]
    message: str
    phase: Literal["commentary", "final_answer"] | None = None
    images: list[str] | None = None
    local_images: list[str] | None = None
    text_elements: list[dict[str, object]] | None = None


class CodexRolloutLine(NativeRecord):
    """Common timestamped envelope for every Codex rollout item."""

    timestamp: str | None = None
    ordinal: int | None = None
    type: str
    payload: SerializeAsAny[CodexPayload] | None = None


class CodexGeneratedRolloutLine(StrictNativeRecord):
    """Strict timestamped envelope shared by generated rollout records."""

    timestamp: str
    ordinal: int | None = None


class CodexSessionMetaLine(CodexGeneratedRolloutLine):
    """Strict generated Codex session metadata line."""

    type: Literal["session_meta"] = "session_meta"
    payload: CodexSessionMetaPayload


class CodexResponseMessageLine(CodexGeneratedRolloutLine):
    """Strict generated Codex model-message line."""

    type: Literal["response_item"] = "response_item"
    payload: CodexResponseMessagePayload


class CodexEventMessageLine(CodexGeneratedRolloutLine):
    """Strict generated Codex user-facing message line."""

    type: Literal["event_msg"] = "event_msg"
    payload: CodexEventMessagePayload
