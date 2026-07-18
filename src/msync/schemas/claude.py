"""Pydantic models for Claude Code JSONL conversation records."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from msync.schemas.base import NativeRecord


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


class ClaudeUserMessage(ClaudeMessage):
    """A user message generated for a resumable Claude session."""

    role: Literal["user"] = "user"


class ClaudeAssistantMessage(ClaudeMessage):
    """An assistant message generated for a resumable Claude session."""

    role: Literal["assistant"] = "assistant"
    type: Literal["message"] = "message"
    content: list[ClaudeContentBlock]
    stop_reason: str | None = "end_turn"
    usage: ClaudeUsage = Field(default_factory=ClaudeUsage)


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


class ClaudeUserRecord(ClaudeRecord):
    """Strict user record emitted by msync's Claude writer."""

    type: Literal["user"] = "user"
    session_id: str = Field(alias="sessionId")
    uuid: str
    timestamp: str
    cwd: str
    message: ClaudeUserMessage
    is_sidechain: bool = Field(default=False, alias="isSidechain")
    user_type: str = Field(default="external", alias="userType")


class ClaudeAssistantRecord(ClaudeRecord):
    """Strict assistant record emitted by msync's Claude writer."""

    type: Literal["assistant"] = "assistant"
    session_id: str = Field(alias="sessionId")
    uuid: str
    timestamp: str
    cwd: str
    message: ClaudeAssistantMessage
    is_sidechain: bool = Field(default=False, alias="isSidechain")
    user_type: str = Field(default="external", alias="userType")
