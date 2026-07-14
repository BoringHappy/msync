"""SQLAlchemy-backed, idempotent transcript archiving."""

from __future__ import annotations

import hashlib
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from sqlalchemy import URL, create_engine, delete, event, func, inspect, select
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.orm import Session

from msync.models import Conversation
from msync.providers import HistoryProvider, get_provider
from msync.tables import (
    Base,
    ConversationRow,
    EventRow,
    LocationRow,
    MessagePartRow,
    SchemaInfoRow,
)

SCHEMA_VERSION = 3

SQLITE_FTS_SCHEMA = """
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


@dataclass(slots=True, frozen=True)
class SearchResult:
    """One archived event returned for search or inspection."""

    provider: str
    conversation_id: str
    title: str | None
    relative_path: str
    role: str | None
    occurred_at: str | None
    text: str


@dataclass(slots=True, frozen=True)
class ArchivedConversation:
    """One stored transcript together with its source-location identity."""

    location_id: int
    source_root: str
    source_mtime_ns: int
    conversation: Conversation


class Archive:
    """A durable archive that supports SQLite, PostgreSQL, and MySQL."""

    def __init__(self, database: str | Path) -> None:
        self.url, self.sqlite_path = _normalize_database(database)
        self.initialized_new_database = False
        if self.sqlite_path is not None:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(self.url, pool_pre_ping=True)
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine, "connect", _configure_sqlite)
        try:
            self._initialize()
        except Exception:
            self.close()
            raise

    @property
    def display_database(self) -> str:
        """Return a human-friendly database target without exposing passwords."""

        if self.sqlite_path is not None:
            return str(self.sqlite_path)
        return self.url.render_as_string(hide_password=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release pooled database connections."""

        self.engine.dispose()

    def upload(
        self,
        *,
        root: Path,
        provider: HistoryProvider,
        transcripts: list[Path],
    ) -> UploadResult:
        """Archive changed transcripts from a single source location."""

        root = root.resolve()
        with Session(self.engine) as session, session.begin():
            location = self._upsert_location(session, root, provider.name)
            result = UploadResult(location_id=location.id, scanned=len(transcripts))
            for path in transcripts:
                self._upload_file(session, result, root, provider, path)
            location.last_scanned_at = datetime.now(UTC)
        return result

    def search(self, search_text: str) -> list[SearchResult]:
        """Find archived events containing the supplied text."""

        statement = (
            select(
                LocationRow.provider,
                ConversationRow.external_id,
                ConversationRow.title,
                ConversationRow.relative_path,
                EventRow.role,
                EventRow.occurred_at,
                EventRow.searchable_text,
            )
            .join(ConversationRow, EventRow.conversation_id == ConversationRow.id)
            .join(LocationRow, ConversationRow.location_id == LocationRow.id)
            .where(EventRow.searchable_text.like(f"%{search_text}%"))
            .order_by(EventRow.occurred_at.desc(), EventRow.id.desc())
        )
        with Session(self.engine) as session:
            return [SearchResult(*row) for row in session.execute(statement)]

    def sample(self, limit: int) -> list[SearchResult]:
        """Return a random selection of non-empty archived message events."""

        if limit < 1:
            raise ValueError("Sample limit must be greater than zero.")
        random_order = func.rand() if self.engine.dialect.name == "mysql" else func.random()
        statement = (
            select(
                LocationRow.provider,
                ConversationRow.external_id,
                ConversationRow.title,
                ConversationRow.relative_path,
                EventRow.role,
                EventRow.occurred_at,
                EventRow.searchable_text,
            )
            .join(ConversationRow, EventRow.conversation_id == ConversationRow.id)
            .join(LocationRow, ConversationRow.location_id == LocationRow.id)
            .where(EventRow.searchable_text != "")
            .order_by(random_order)
            .limit(limit)
        )
        with Session(self.engine) as session:
            return [SearchResult(*row) for row in session.execute(statement)]

    def conversations(self) -> list[ArchivedConversation]:
        """Reconstruct every stored conversation from its lossless transcript blob."""

        statement = (
            select(
                LocationRow.id,
                LocationRow.root_path,
                LocationRow.provider,
                ConversationRow.relative_path,
                ConversationRow.source_mtime_ns,
                ConversationRow.content_sha256,
                ConversationRow.transcript_codec,
                ConversationRow.transcript,
            )
            .join(ConversationRow, ConversationRow.location_id == LocationRow.id)
            .order_by(LocationRow.id, ConversationRow.relative_path)
        )
        with Session(self.engine) as session:
            rows = list(session.execute(statement))

        conversations: list[ArchivedConversation] = []
        for row in rows:
            if row.transcript_codec != "zlib":
                raise RuntimeError(
                    f"Unsupported transcript codec {row.transcript_codec!r} in archive."
                )
            try:
                transcript = zlib.decompress(row.transcript)
            except zlib.error as error:
                raise RuntimeError(
                    "A stored transcript is corrupt and cannot be decompressed."
                ) from error
            if hashlib.sha256(transcript).hexdigest() != row.content_sha256:
                raise RuntimeError("A stored transcript failed its SHA-256 integrity check.")

            root = Path(row.root_path)
            path = root / Path(row.relative_path)
            provider = get_provider(row.provider)
            conversation = provider.read(path, root, transcript=transcript)
            conversations.append(
                ArchivedConversation(
                    location_id=row.id,
                    source_root=row.root_path,
                    source_mtime_ns=row.source_mtime_ns,
                    conversation=conversation,
                )
            )
        return conversations

    def _initialize(self) -> None:
        with self.engine.begin() as connection:
            expected_tables = set(Base.metadata.tables)
            existing_tables = set(inspect(connection).get_table_names())
            self.initialized_new_database = not expected_tables.intersection(existing_tables)
            sqlite_version = 0
            if self.engine.dialect.name == "sqlite":
                sqlite_version = int(connection.exec_driver_sql("PRAGMA user_version").scalar_one())
                if sqlite_version not in {0, SCHEMA_VERSION}:
                    _raise_incompatible_schema(sqlite_version)

            Base.metadata.create_all(connection)
            self._validate_schema(connection)
            stored_version = connection.execute(
                select(SchemaInfoRow.value).where(SchemaInfoRow.key == "schema_version")
            ).scalar_one_or_none()
            if stored_version is not None and int(stored_version) != SCHEMA_VERSION:
                _raise_incompatible_schema(int(stored_version))
            if stored_version is None:
                connection.execute(
                    SchemaInfoRow.__table__.insert().values(
                        key="schema_version", value=str(SCHEMA_VERSION)
                    )
                )

            if self.engine.dialect.name == "sqlite":
                for statement in _sqlite_statements(SQLITE_FTS_SCHEMA):
                    connection.exec_driver_sql(statement)
                self._validate_sqlite_fts(connection)
                connection.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _validate_schema(connection: Connection) -> None:
        """Ensure the loaded database matches every portable ORM mapping."""

        inspector = inspect(connection)
        actual_tables = set(inspector.get_table_names())
        expected_tables = set(Base.metadata.tables)
        problems = (
            [f"missing tables: {', '.join(sorted(expected_tables - actual_tables))}"]
            if (expected_tables - actual_tables)
            else []
        )

        for table in Base.metadata.sorted_tables:
            if table.name not in actual_tables:
                continue
            actual_columns = {column["name"] for column in inspector.get_columns(table.name)}
            expected_columns = set(table.columns.keys())
            if missing_columns := expected_columns - actual_columns:
                problems.append(
                    f"{table.name} missing columns: {', '.join(sorted(missing_columns))}"
                )

            expected_primary_key = tuple(column.name for column in table.primary_key.columns)
            actual_primary_key = tuple(
                inspector.get_pk_constraint(table.name).get("constrained_columns") or ()
            )
            if expected_primary_key != actual_primary_key:
                problems.append(
                    f"{table.name} primary key is {actual_primary_key}, "
                    f"expected {expected_primary_key}"
                )

            actual_unique_columns = {
                tuple(constraint.get("column_names") or ())
                for constraint in inspector.get_unique_constraints(table.name)
            }
            actual_unique_columns.update(
                tuple(index.get("column_names") or ())
                for index in inspector.get_indexes(table.name)
                if index.get("unique")
            )
            for index in table.indexes:
                expected_index = tuple(column.name for column in index.columns)
                if index.unique and expected_index not in actual_unique_columns:
                    problems.append(f"{table.name} missing unique index on {expected_index}")

            actual_foreign_keys = {
                (
                    tuple(foreign_key.get("constrained_columns") or ()),
                    foreign_key.get("referred_table"),
                )
                for foreign_key in inspector.get_foreign_keys(table.name)
            }
            for foreign_key in table.foreign_key_constraints:
                expected_foreign_key = (
                    tuple(column.name for column in foreign_key.columns),
                    foreign_key.referred_table.name,
                )
                if expected_foreign_key not in actual_foreign_keys:
                    problems.append(f"{table.name} missing foreign key {expected_foreign_key}")

        if problems:
            raise RuntimeError("Database schema validation failed: " + "; ".join(problems))

    @staticmethod
    def _validate_sqlite_fts(connection: Connection) -> None:
        rows = connection.exec_driver_sql(
            """
            SELECT type, name FROM sqlite_master
            WHERE name IN (
                'events_fts', 'events_fts_insert', 'events_fts_delete', 'events_fts_update'
            )
            """
        )
        actual_objects = {(row[0], row[1]) for row in rows}
        expected_objects = {
            ("table", "events_fts"),
            ("trigger", "events_fts_insert"),
            ("trigger", "events_fts_delete"),
            ("trigger", "events_fts_update"),
        }
        if missing_objects := expected_objects - actual_objects:
            missing = ", ".join(name for _, name in sorted(missing_objects))
            raise RuntimeError(f"Database schema validation failed: missing SQLite FTS: {missing}")

    @staticmethod
    def _upsert_location(session: Session, root: Path, provider: str) -> LocationRow:
        root_path = str(root)
        root_path_hash = _text_hash(root_path)
        location = session.scalar(
            select(LocationRow).where(LocationRow.root_path_hash == root_path_hash)
        )
        if location is None:
            location = LocationRow(
                provider=provider,
                root_path=root_path,
                root_path_hash=root_path_hash,
                display_name=root.name or root_path,
            )
            session.add(location)
            session.flush()
            return location
        if location.root_path != root_path:
            raise RuntimeError("A SHA-256 collision occurred while identifying a source location.")
        if location.provider != provider:
            session.execute(
                delete(ConversationRow).where(ConversationRow.location_id == location.id)
            )
            location.provider = provider
        location.display_name = root.name or root_path
        return location

    @staticmethod
    def _upload_file(
        session: Session,
        result: UploadResult,
        root: Path,
        provider: HistoryProvider,
        path: Path,
    ) -> None:
        transcript = path.read_bytes()
        content_sha256 = hashlib.sha256(transcript).hexdigest()
        relative_path = path.relative_to(root).as_posix()
        relative_path_hash = _text_hash(relative_path)
        existing = session.scalar(
            select(ConversationRow).where(
                ConversationRow.location_id == result.location_id,
                ConversationRow.relative_path_hash == relative_path_hash,
            )
        )
        if existing is not None and existing.relative_path != relative_path:
            raise RuntimeError("A SHA-256 collision occurred while identifying a transcript.")
        if existing is not None and existing.content_sha256 == content_sha256:
            result.unchanged += 1
            return

        conversation = provider.read(path, root, transcript=transcript)
        stat = path.stat()
        if existing is None:
            existing = ConversationRow(
                location_id=result.location_id,
                external_id=conversation.external_id,
                relative_path=relative_path,
                relative_path_hash=relative_path_hash,
            )
            session.add(existing)
            result.imported += 1
        else:
            session.execute(delete(EventRow).where(EventRow.conversation_id == existing.id))
            result.updated += 1

        _update_conversation(existing, conversation, stat.st_mtime_ns)
        session.flush()
        Archive._insert_events(session, existing.id, conversation, result)

    @staticmethod
    def _insert_events(
        session: Session,
        conversation_id: int,
        conversation: Conversation,
        result: UploadResult,
    ) -> None:
        rows = [
            EventRow(
                conversation_id=conversation_id,
                sequence=source.sequence,
                external_id=source.external_id,
                parent_external_id=source.parent_external_id,
                event_type=source.event_type,
                event_subtype=source.event_subtype,
                role=source.role,
                visibility=source.visibility,
                occurred_at=source.occurred_at,
                searchable_text=source.searchable_text,
                raw_json=source.raw_json,
                parse_error=source.parse_error,
            )
            for source in conversation.events
        ]
        session.add_all(rows)
        session.flush()
        parts = [
            MessagePartRow(
                event_id=row.id,
                sequence=part.sequence,
                content_type=part.content_type,
                text=part.text,
                raw_json=part.raw_json,
            )
            for row, source in zip(rows, conversation.events, strict=True)
            for part in source.parts
        ]
        session.add_all(parts)
        result.events += len(rows)
        result.message_parts += len(parts)


def _update_conversation(
    row: ConversationRow, conversation: Conversation, source_mtime_ns: int
) -> None:
    row.external_id = conversation.external_id
    row.conversation_kind = conversation.kind
    row.parent_external_id = conversation.parent_external_id
    row.title = conversation.title
    row.cwd = conversation.cwd
    row.model = conversation.model
    row.git_branch = conversation.git_branch
    row.started_at = conversation.started_at
    row.ended_at = conversation.ended_at
    row.source_mtime_ns = source_mtime_ns
    row.source_size = len(conversation.transcript)
    row.content_sha256 = conversation.sha256
    row.transcript_codec = "zlib"
    row.transcript = zlib.compress(conversation.transcript)
    row.metadata_json = conversation.metadata
    row.imported_at = datetime.now(UTC)


def _normalize_database(database: str | Path) -> tuple[URL, Path | None]:
    raw = str(database)
    if isinstance(database, Path) or "://" not in raw:
        path = Path(raw).expanduser().resolve()
        return URL.create("sqlite+pysqlite", database=str(path)), path

    url = make_url(raw)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    elif url.drivername == "mysql":
        url = url.set(drivername="mysql+pymysql")

    if url.get_backend_name() != "sqlite" or url.database in {None, "", ":memory:"}:
        return url, None
    path = Path(url.database).expanduser().resolve()
    return url.set(database=str(path)), path


def _configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
    finally:
        cursor.close()


def _sqlite_statements(script: str) -> list[str]:
    """Split the fixed FTS DDL into complete SQLite statements."""

    statements: list[str] = []
    current: list[str] = []
    for line in script.splitlines():
        current.append(line)
        candidate = "\n".join(current).strip()
        if candidate.endswith(";") and (
            "CREATE TRIGGER" not in candidate or candidate.endswith("END;")
        ):
            statements.append(candidate)
            current = []
    return statements


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _raise_incompatible_schema(version: int) -> None:
    raise RuntimeError(
        f"Database schema version {version} is incompatible with this msync schema "
        f"({SCHEMA_VERSION}); create a new database."
    )
