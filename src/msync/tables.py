"""Portable SQLAlchemy mappings for the msync archive."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ID_TYPE = BigInteger().with_variant(Integer, "sqlite")
LONG_TEXT = Text()
LONG_BINARY = LargeBinary()


class Base(DeclarativeBase):
    """Base class for archive mappings."""


class SchemaInfoRow(Base):
    """Application-level schema metadata shared by every backend."""

    __tablename__ = "schema_info"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)


class LocationRow(Base):
    """A physical Claude or Codex history directory on one host."""

    __tablename__ = "locations"
    __table_args__ = (Index("locations_root_path_hash_uq", "root_path_hash", unique=True),)

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    account_username: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    root_path: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    root_path_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationRow(Base):
    """A complete source transcript and its session-level metadata."""

    __tablename__ = "conversations"
    __table_args__ = (
        Index(
            "conversations_location_path_hash_uq",
            "location_id",
            "relative_path_hash",
            unique=True,
        ),
        Index(
            "conversations_logical_revision_uq",
            "account_username",
            "logical_session_id",
            "chat_sha256",
            unique=True,
        ),
        Index("conversations_external_id_idx", "location_id", "external_id"),
        Index("conversations_time_idx", "started_at", "ended_at"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    account_username: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    location_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("locations.id", ondelete="CASCADE"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    relative_path: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    relative_path_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    logical_session_id: Mapped[str | None] = mapped_column(String(36))
    chat_sha256: Mapped[str | None] = mapped_column(String(64))
    conversation_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="main")
    parent_external_id: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(512))
    cwd: Mapped[str | None] = mapped_column(LONG_TEXT)
    model: Mapped[str | None] = mapped_column(String(255))
    git_branch: Mapped[str | None] = mapped_column(String(512))
    started_at: Mapped[str | None] = mapped_column(String(64))
    ended_at: Mapped[str | None] = mapped_column(String(64))
    source_mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    transcript_codec: Mapped[str] = mapped_column(String(16), nullable=False, default="zlib")
    transcript: Mapped[bytes] = mapped_column(LONG_BINARY, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ConversationMetricRow(Base):
    """Precomputed dashboard facts for one archived conversation."""

    __tablename__ = "conversation_metrics"
    __table_args__ = (Index("conversation_metrics_activity_idx", "activity_at"),)

    conversation_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reasoning_event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    preview: Mapped[str | None] = mapped_column(LONG_TEXT)
    activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activity_day: Mapped[date | None] = mapped_column(Date)
    activity_hour: Mapped[int | None] = mapped_column(Integer)
    activity_weekday: Mapped[int | None] = mapped_column(Integer)
    duration_minutes: Mapped[float | None] = mapped_column(Float)
    tool_counts_json: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)


class ArchiveRevisionRow(Base):
    """Monotonic per-owner revision used to invalidate dashboard summaries."""

    __tablename__ = "archive_revisions"

    account_username: Mapped[str] = mapped_column(String(255), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EventRow(Base):
    """A source JSONL event with portable search text."""

    __tablename__ = "events"
    __table_args__ = (
        Index("events_conversation_sequence_uq", "conversation_id", "sequence", unique=True),
        Index("events_conversation_time_idx", "conversation_id", "occurred_at"),
        Index("events_external_id_idx", "external_id"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255))
    parent_external_id: Mapped[str | None] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_subtype: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str | None] = mapped_column(String(32))
    visibility: Mapped[str] = mapped_column(String(16), nullable=False)
    occurred_at: Mapped[str | None] = mapped_column(String(64))
    searchable_text: Mapped[str] = mapped_column(LONG_TEXT, nullable=False, default="")
    raw_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    parse_error: Mapped[str | None] = mapped_column(LONG_TEXT)


class MessagePartRow(Base):
    """A structured message content block."""

    __tablename__ = "message_parts"
    __table_args__ = (
        Index("message_parts_event_sequence_uq", "event_id", "sequence", unique=True),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str | None] = mapped_column(LONG_TEXT)
    raw_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
