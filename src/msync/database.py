"""SQLite schema and idempotent transcript archiving."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from msync.models import Conversation, Provider
from msync.readers import read_conversation

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL CHECK (provider IN ('claude', 'codex')),
    root_path TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_scanned_at TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    conversation_kind TEXT NOT NULL DEFAULT 'main',
    parent_external_id TEXT,
    title TEXT,
    cwd TEXT,
    model TEXT,
    git_branch TEXT,
    started_at TEXT,
    ended_at TEXT,
    source_mtime_ns INTEGER NOT NULL,
    source_size INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    transcript_codec TEXT NOT NULL DEFAULT 'zlib' CHECK (transcript_codec = 'zlib'),
    transcript BLOB NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (location_id, relative_path)
) STRICT;

CREATE INDEX IF NOT EXISTS conversations_external_id_idx
    ON conversations(location_id, external_id);
CREATE INDEX IF NOT EXISTS conversations_time_idx
    ON conversations(started_at, ended_at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    external_id TEXT,
    parent_external_id TEXT,
    event_type TEXT NOT NULL,
    event_subtype TEXT,
    role TEXT,
    visibility TEXT NOT NULL CHECK (visibility IN ('display', 'model', 'metadata')),
    occurred_at TEXT,
    searchable_text TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL,
    parse_error TEXT,
    UNIQUE (conversation_id, sequence)
) STRICT;

CREATE INDEX IF NOT EXISTS events_conversation_time_idx
    ON events(conversation_id, occurred_at);
CREATE INDEX IF NOT EXISTS events_external_id_idx ON events(external_id);

CREATE TABLE IF NOT EXISTS message_parts (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    text TEXT,
    raw_json TEXT NOT NULL,
    UNIQUE (event_id, sequence)
) STRICT;

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    searchable_text,
    role UNINDEXED,
    conversation_id UNINDEXED,
    content='events',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS events_fts_insert AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, searchable_text, role, conversation_id)
    VALUES (new.id, new.searchable_text, new.role, new.conversation_id);
END;

CREATE TRIGGER IF NOT EXISTS events_fts_delete AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, searchable_text, role, conversation_id)
    VALUES ('delete', old.id, old.searchable_text, old.role, old.conversation_id);
END;

CREATE TRIGGER IF NOT EXISTS events_fts_update AFTER UPDATE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, searchable_text, role, conversation_id)
    VALUES ('delete', old.id, old.searchable_text, old.role, old.conversation_id);
    INSERT INTO events_fts(rowid, searchable_text, role, conversation_id)
    VALUES (new.id, new.searchable_text, new.role, new.conversation_id);
END;
"""


@dataclass(slots=True)
class UploadResult:
    """Counts collected during one directory upload."""

    location_id: int
    scanned: int = 0
    imported: int = 0
    updated: int = 0
    unchanged: int = 0
    events: int = 0
    message_parts: int = 0


class Archive:
    """A durable SQLite archive for one or more provider locations."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            self._initialize(connection)
        except Exception:
            connection.close()
            raise
        return connection

    def upload(
        self,
        *,
        root: Path,
        provider: Provider,
        transcripts: list[Path],
    ) -> UploadResult:
        """Archive changed transcripts from a single source location."""

        root = root.resolve()
        with closing(self.connect()) as connection, connection:
            location_id = self._upsert_location(connection, root, provider)
            result = UploadResult(location_id=location_id, scanned=len(transcripts))
            for path in transcripts:
                self._upload_file(connection, result, root, provider, path)
            connection.execute(
                """
                UPDATE locations
                SET last_scanned_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (location_id,),
            )
        return result

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {current_version} is newer than this msync supports "
                f"({SCHEMA_VERSION})."
            )
        if current_version == 0:
            connection.executescript(SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _upsert_location(connection: sqlite3.Connection, root: Path, provider: Provider) -> int:
        root_path = str(root)
        connection.execute(
            """
            INSERT INTO locations(provider, root_path, display_name)
            VALUES (?, ?, ?)
            ON CONFLICT(root_path) DO UPDATE SET
                provider = excluded.provider,
                display_name = excluded.display_name
            """,
            (provider, root_path, root.name or root_path),
        )
        row = connection.execute(
            "SELECT id FROM locations WHERE root_path = ?", (root_path,)
        ).fetchone()
        assert row is not None
        return int(row["id"])

    @staticmethod
    def _upload_file(
        connection: sqlite3.Connection,
        result: UploadResult,
        root: Path,
        provider: Provider,
        path: Path,
    ) -> None:
        transcript = path.read_bytes()
        content_sha256 = hashlib.sha256(transcript).hexdigest()
        relative_path = path.relative_to(root).as_posix()
        existing = connection.execute(
            """
            SELECT id, content_sha256 FROM conversations
            WHERE location_id = ? AND relative_path = ?
            """,
            (result.location_id, relative_path),
        ).fetchone()
        if existing is not None and existing["content_sha256"] == content_sha256:
            result.unchanged += 1
            return

        conversation = read_conversation(path, root, provider, transcript=transcript)
        stat = path.stat()
        conversation_id = Archive._upsert_conversation(
            connection, result.location_id, conversation, stat.st_mtime_ns
        )
        connection.execute("DELETE FROM events WHERE conversation_id = ?", (conversation_id,))
        Archive._insert_events(connection, conversation_id, conversation, result)
        if existing is None:
            result.imported += 1
        else:
            result.updated += 1

    @staticmethod
    def _upsert_conversation(
        connection: sqlite3.Connection,
        location_id: int,
        conversation: Conversation,
        source_mtime_ns: int,
    ) -> int:
        values = (
            location_id,
            conversation.external_id,
            conversation.relative_path,
            conversation.kind,
            conversation.parent_external_id,
            conversation.title,
            conversation.cwd,
            conversation.model,
            conversation.git_branch,
            conversation.started_at,
            conversation.ended_at,
            source_mtime_ns,
            len(conversation.transcript),
            conversation.sha256,
            zlib.compress(conversation.transcript),
            json.dumps(conversation.metadata, ensure_ascii=False, separators=(",", ":")),
        )
        row = connection.execute(
            """
            INSERT INTO conversations(
                location_id, external_id, relative_path, conversation_kind,
                parent_external_id, title, cwd, model, git_branch, started_at,
                ended_at, source_mtime_ns, source_size, content_sha256,
                transcript, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(location_id, relative_path) DO UPDATE SET
                external_id = excluded.external_id,
                conversation_kind = excluded.conversation_kind,
                parent_external_id = excluded.parent_external_id,
                title = excluded.title,
                cwd = excluded.cwd,
                model = excluded.model,
                git_branch = excluded.git_branch,
                started_at = excluded.started_at,
                ended_at = excluded.ended_at,
                source_mtime_ns = excluded.source_mtime_ns,
                source_size = excluded.source_size,
                content_sha256 = excluded.content_sha256,
                transcript = excluded.transcript,
                metadata_json = excluded.metadata_json,
                imported_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            RETURNING id
            """,
            values,
        ).fetchone()
        assert row is not None
        return int(row["id"])

    @staticmethod
    def _insert_events(
        connection: sqlite3.Connection,
        conversation_id: int,
        conversation: Conversation,
        result: UploadResult,
    ) -> None:
        connection.executemany(
            """
            INSERT INTO events(
                conversation_id, sequence, external_id, parent_external_id,
                event_type, event_subtype, role, visibility, occurred_at,
                searchable_text, raw_json, parse_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    conversation_id,
                    event.sequence,
                    event.external_id,
                    event.parent_external_id,
                    event.event_type,
                    event.event_subtype,
                    event.role,
                    event.visibility,
                    event.occurred_at,
                    event.searchable_text,
                    event.raw_json,
                    event.parse_error,
                )
                for event in conversation.events
            ),
        )
        event_ids = {
            int(row["sequence"]): int(row["id"])
            for row in connection.execute(
                "SELECT id, sequence FROM events WHERE conversation_id = ?",
                (conversation_id,),
            )
        }
        connection.executemany(
            """
            INSERT INTO message_parts(event_id, sequence, content_type, text, raw_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                (
                    event_ids[event.sequence],
                    part.sequence,
                    part.content_type,
                    part.text,
                    part.raw_json,
                )
                for event in conversation.events
                for part in event.parts
            ),
        )
        result.events += len(conversation.events)
        result.message_parts += sum(len(event.parts) for event in conversation.events)
