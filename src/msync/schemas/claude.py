"""Pydantic models for Claude Code JSONL conversation records."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import Field, RootModel, model_validator

from msync.schemas.base import NativeRecord, StrictNativeRecord


class ClaudeContentBlock(NativeRecord):
    """One structured Claude message content block."""

    type: str
    text: str | None = None


type ClaudeContent = str | list[str | ClaudeContentBlock]


class ClaudeUsage(NativeRecord):
    """Token accounting attached to an assistant message."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class ClaudeSyncProvenance(NativeRecord):
    """Stable msync identity retained across provider conversions."""

    source_provider: str | None = Field(default=None, alias="sourceProvider")
    source_conversation_id: str | None = Field(default=None, alias="sourceConversationId")
    source_key: str | None = Field(default=None, alias="sourceKey")
    logical_session_id: str | None = Field(default=None, alias="logicalSessionId")


class ClaudeMessage(NativeRecord):
    """Provider message nested inside a Claude transcript record."""

    role: str
    content: ClaudeContent
    id: str | None = None
    type: str | None = None
    model: str | None = None
    stop_reason: str | None = None
    stop_sequence: str | None = None
    usage: ClaudeUsage | None = None


class ClaudeUserMessage(StrictNativeRecord):
    """A user message generated for a resumable Claude session."""

    role: Literal["user"] = "user"
    content: str


class ClaudeGeneratedContentBlock(StrictNativeRecord):
    """A text block emitted in a generated Claude assistant message."""

    type: Literal["text"] = "text"
    text: str


class ClaudeGeneratedUsage(StrictNativeRecord):
    """Token accounting emitted for a generated Claude assistant message."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class ClaudeAssistantMessage(StrictNativeRecord):
    """An assistant message generated for a resumable Claude session."""

    role: Literal["assistant"] = "assistant"
    type: Literal["message"] = "message"
    id: str
    model: str
    content: list[ClaudeGeneratedContentBlock]
    stop_reason: str | None = "end_turn"
    stop_sequence: str | None = None
    usage: ClaudeGeneratedUsage = Field(default_factory=ClaudeGeneratedUsage)


class ClaudeRecord(NativeRecord):
    """Common envelope accepted from any Claude Code JSONL record."""

    type: str
    session_id: str | None = Field(default=None, alias="sessionId")
    uuid: str | None = None
    parent_uuid: str | None = Field(default=None, alias="parentUuid")
    timestamp: str | None = None
    cwd: str | None = None
    git_branch: str | None = Field(default=None, alias="gitBranch")
    version: str | None = None
    entrypoint: str | None = None
    is_meta: bool | None = Field(default=None, alias="isMeta")
    is_sidechain: bool | None = Field(default=None, alias="isSidechain")
    user_type: str | None = Field(default=None, alias="userType")
    source_tool_use_id: str | None = Field(default=None, alias="sourceToolUseID")
    agent_id: str | None = Field(default=None, alias="agentId")
    message: ClaudeMessage | None = None
    content: Any = None
    summary: str | None = None
    subtype: str | None = None
    msync: ClaudeSyncProvenance | None = None


class ClaudeGeneratedRecord(StrictNativeRecord):
    """Fields shared by every transcript record emitted by msync."""

    session_id: UUID = Field(alias="sessionId")
    uuid: UUID
    parent_uuid: UUID | None = Field(default=None, alias="parentUuid")
    timestamp: str
    cwd: str
    git_branch: str | None = Field(default=None, alias="gitBranch")
    version: str | None = Field(default=None, pattern=r"^\d+\.\d+\.\d+(?:[-+].+)?$")
    entrypoint: Literal["cli"] | None = None
    is_sidechain: bool = Field(default=False, alias="isSidechain")
    user_type: Literal["external"] = Field(default="external", alias="userType")


class ClaudeUserRecord(ClaudeGeneratedRecord):
    """Strict user record emitted by msync's Claude writer."""

    type: Literal["user"] = "user"
    message: ClaudeUserMessage


class ClaudeAssistantRecord(ClaudeGeneratedRecord):
    """Strict assistant record emitted by msync's Claude writer."""

    type: Literal["assistant"] = "assistant"
    message: ClaudeAssistantMessage


type ClaudeGeneratedTranscriptRecord = Annotated[
    ClaudeUserRecord | ClaudeAssistantRecord,
    Field(discriminator="type"),
]


class ClaudeGeneratedTranscript(RootModel[list[ClaudeGeneratedTranscriptRecord]]):
    """A generated message chain that Claude Code can resume."""

    @model_validator(mode="after")
    def require_resumable_message_chain(self) -> Self:
        """Keep every record reachable through Claude's parent UUID chain."""

        if not self.root:
            raise ValueError("generated Claude transcript contains no records")

        session_id = self.root[0].session_id
        parent_uuid: UUID | None = None
        seen_uuids: set[UUID] = set()
        for record in self.root:
            if record.session_id != session_id:
                raise ValueError("all generated records must use the same sessionId")
            if record.uuid in seen_uuids:
                raise ValueError("generated record UUIDs must be unique")
            if record.parent_uuid != parent_uuid:
                raise ValueError("generated parentUuid chain is not contiguous")
            seen_uuids.add(record.uuid)
            parent_uuid = record.uuid
        return self
