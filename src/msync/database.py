"""SQLAlchemy-backed, idempotent transcript archiving."""

from __future__ import annotations

import hashlib
import socket
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from sqlalchemy import URL, case, create_engine, delete, event, func, inspect, or_, select, update
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
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

SCHEMA_VERSION = 6
LEGACY_HOSTNAME = "unknown"

SchemaUpgradeReporter = Callable[[int, int], None]
SchemaUpgradeProgressReporter = Callable[[int, int], None]
SchemaMigrationFunction = Callable[
    [Connection, SchemaUpgradeProgressReporter | None],
    None,
]


@dataclass(frozen=True, slots=True)
class SchemaMigration:
    """One sequential archive schema migration."""

    target_version: int
    upgrade: SchemaMigrationFunction


class SchemaUpgradeRequiredError(RuntimeError):
    """Raised when an archive must be upgraded before it can be opened."""

    def __init__(self, current_version: int) -> None:
        self.current_version = current_version
        self.target_version = SCHEMA_VERSION
        super().__init__(
            f"Database schema version {current_version} must be upgraded to {SCHEMA_VERSION}. "
            "Stop other msync processes, then run `msync upgrade --database <database>`."
        )


class SchemaUpgradeBlockedError(RuntimeError):
    """Raised when another database transaction prevents a schema upgrade."""

    def __init__(self, current_version: int) -> None:
        self.current_version = current_version
        self.target_version = SCHEMA_VERSION
        super().__init__(
            f"Database schema upgrade from {current_version} to {SCHEMA_VERSION} is blocked by "
            "another transaction. Stop or finish other msync uploads and servers, then retry "
            "`msync upgrade`."
        )


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
    duplicates: int = 0
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


@dataclass(slots=True, frozen=True)
class ArchiveLocation:
    """A source location available to the history browser."""

    id: int
    provider: str
    hostname: str
    root_path: str
    display_name: str
    last_scanned_at: datetime | None
    conversation_count: int


@dataclass(slots=True, frozen=True)
class ConversationSummary:
    """Compact conversation metadata used by the history browser list."""

    id: int
    location_id: int
    provider: str
    hostname: str
    external_id: str
    title: str | None
    conversation_kind: str
    cwd: str | None
    model: str | None
    git_branch: str | None
    started_at: str | None
    ended_at: str | None
    event_count: int
    message_count: int
    preview: str | None


@dataclass(slots=True, frozen=True)
class ArchivedMessagePart:
    """One structured content block displayed in expanded event details."""

    sequence: int
    content_type: str
    text: str | None
    raw_json: str


@dataclass(slots=True, frozen=True)
class ArchivedEvent:
    """One normalized event plus its lossless source representation."""

    sequence: int
    external_id: str | None
    parent_external_id: str | None
    event_type: str
    event_subtype: str | None
    role: str | None
    visibility: str
    occurred_at: str | None
    text: str
    raw_json: str
    parse_error: str | None
    parts: tuple[ArchivedMessagePart, ...]


@dataclass(slots=True, frozen=True)
class ConversationDetail:
    """A complete browser view of an archived conversation."""

    summary: ConversationSummary
    relative_path: str
    parent_external_id: str | None
    metadata: dict[str, Any]
    events: tuple[ArchivedEvent, ...]


class Archive:
    """A durable archive that supports SQLite, PostgreSQL, and MySQL."""

    def __init__(
        self,
        database: str | Path,
        *,
        hostname: str | None = None,
        auto_upgrade: bool = True,
        schema_lock_timeout: int | None = None,
        upgrade_reporter: SchemaUpgradeReporter | None = None,
        upgrade_progress_reporter: SchemaUpgradeProgressReporter | None = None,
    ) -> None:
        if schema_lock_timeout is not None and schema_lock_timeout < 1:
            raise ValueError("Schema lock timeout must be at least one second.")
        self.hostname = _normalize_hostname(hostname)
        self.url, self.sqlite_path = _normalize_database(database)
        self.auto_upgrade = auto_upgrade
        self.schema_lock_timeout = schema_lock_timeout
        self.upgrade_reporter = upgrade_reporter
        self.upgrade_progress_reporter = upgrade_progress_reporter
        self.initialized_new_database = False
        self.schema_version_before: int | None = None
        self.schema_version = SCHEMA_VERSION
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
            location = self._upsert_location(session, root, provider.name, self.hostname)
            result = UploadResult(location_id=location.id, scanned=len(transcripts))
            for path in transcripts:
                for attempt in range(2):
                    file_result = UploadResult(location_id=location.id)
                    try:
                        with session.begin_nested():
                            self._upload_file(session, file_result, root, provider, path)
                    except IntegrityError:
                        if attempt:
                            raise
                        continue
                    _accumulate_upload_result(result, file_result)
                    break
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

    def browse_locations(self) -> list[ArchiveLocation]:
        """Return source locations and their conversation counts for the web UI."""

        statement = (
            select(
                LocationRow.id,
                LocationRow.provider,
                LocationRow.hostname,
                LocationRow.root_path,
                LocationRow.display_name,
                LocationRow.last_scanned_at,
                func.count(ConversationRow.id),
            )
            .outerjoin(ConversationRow, ConversationRow.location_id == LocationRow.id)
            .group_by(
                LocationRow.id,
                LocationRow.provider,
                LocationRow.hostname,
                LocationRow.root_path,
                LocationRow.display_name,
                LocationRow.last_scanned_at,
            )
            .order_by(LocationRow.display_name, LocationRow.id)
        )
        with Session(self.engine) as session:
            return [ArchiveLocation(*row) for row in session.execute(statement)]

    def browse_conversations(
        self,
        *,
        location_id: int | None = None,
        search_text: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> list[ConversationSummary]:
        """Return recent conversations, optionally filtered by location and text."""

        if limit < 1 or limit > 500:
            raise ValueError("Conversation limit must be between 1 and 500.")
        if offset < 0:
            raise ValueError("Conversation offset must not be negative.")

        event_stats = (
            select(
                EventRow.conversation_id.label("conversation_id"),
                func.count(EventRow.id).label("event_count"),
                func.sum(
                    case(
                        (
                            EventRow.role.in_(("user", "assistant"))
                            & (EventRow.searchable_text != ""),
                            1,
                        ),
                        else_=0,
                    )
                ).label("message_count"),
            )
            .group_by(EventRow.conversation_id)
            .subquery()
        )
        preview = (
            select(EventRow.searchable_text)
            .where(
                EventRow.conversation_id == ConversationRow.id,
                EventRow.role == "user",
                EventRow.searchable_text != "",
            )
            .order_by(EventRow.sequence)
            .limit(1)
            .correlate(ConversationRow)
            .scalar_subquery()
        )
        statement = (
            select(
                ConversationRow.id,
                ConversationRow.location_id,
                LocationRow.provider,
                LocationRow.hostname,
                ConversationRow.external_id,
                ConversationRow.title,
                ConversationRow.conversation_kind,
                ConversationRow.cwd,
                ConversationRow.model,
                ConversationRow.git_branch,
                ConversationRow.started_at,
                ConversationRow.ended_at,
                func.coalesce(event_stats.c.event_count, 0),
                func.coalesce(event_stats.c.message_count, 0),
                preview,
            )
            .join(LocationRow, ConversationRow.location_id == LocationRow.id)
            .outerjoin(event_stats, event_stats.c.conversation_id == ConversationRow.id)
        )
        if location_id is not None:
            statement = statement.where(ConversationRow.location_id == location_id)
        if query := search_text.strip():
            pattern = f"%{query}%"
            matching_event = (
                select(EventRow.id)
                .where(
                    EventRow.conversation_id == ConversationRow.id,
                    EventRow.searchable_text.ilike(pattern),
                )
                .exists()
            )
            statement = statement.where(
                or_(
                    ConversationRow.title.ilike(pattern),
                    ConversationRow.external_id.ilike(pattern),
                    ConversationRow.cwd.ilike(pattern),
                    matching_event,
                )
            )
        statement = (
            statement.order_by(
                func.coalesce(ConversationRow.ended_at, ConversationRow.started_at).desc(),
                ConversationRow.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        with Session(self.engine) as session:
            return [ConversationSummary(*row) for row in session.execute(statement)]

    def browse_conversation(
        self,
        conversation_id: int,
        *,
        event_limit: int | None = None,
        event_offset: int = 0,
    ) -> ConversationDetail | None:
        """Return conversation metadata and an optionally bounded event page."""

        if event_limit is not None and (event_limit < 1 or event_limit > 500):
            raise ValueError("Event limit must be between 1 and 500.")
        if event_offset < 0:
            raise ValueError("Event offset must not be negative.")

        statement = (
            select(
                ConversationRow.id,
                ConversationRow.location_id,
                LocationRow.provider,
                LocationRow.hostname,
                ConversationRow.external_id,
                ConversationRow.title,
                ConversationRow.conversation_kind,
                ConversationRow.cwd,
                ConversationRow.model,
                ConversationRow.git_branch,
                ConversationRow.started_at,
                ConversationRow.ended_at,
                ConversationRow.relative_path,
                ConversationRow.parent_external_id,
                ConversationRow.metadata_json,
            )
            .join(LocationRow, ConversationRow.location_id == LocationRow.id)
            .where(ConversationRow.id == conversation_id)
        )
        with Session(self.engine) as session:
            row = session.execute(statement).one_or_none()
            if row is None:
                return None

            event_count, message_count = session.execute(
                select(
                    func.count(EventRow.id),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    EventRow.role.in_(("user", "assistant"))
                                    & (EventRow.searchable_text != ""),
                                    1,
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                ).where(EventRow.conversation_id == conversation_id)
            ).one()
            preview = session.scalar(
                select(EventRow.searchable_text)
                .where(
                    EventRow.conversation_id == conversation_id,
                    EventRow.role == "user",
                    EventRow.searchable_text != "",
                )
                .order_by(EventRow.sequence)
                .limit(1)
            )
            event_statement = (
                select(EventRow)
                .where(EventRow.conversation_id == conversation_id)
                .order_by(EventRow.sequence)
                .offset(event_offset)
            )
            if event_limit is not None:
                event_statement = event_statement.limit(event_limit)
            event_rows = list(session.execute(event_statement).scalars())
            part_rows = (
                list(
                    session.execute(
                        select(MessagePartRow)
                        .where(MessagePartRow.event_id.in_([event.id for event in event_rows]))
                        .order_by(MessagePartRow.event_id, MessagePartRow.sequence)
                    ).scalars()
                )
                if event_rows
                else []
            )

        parts_by_event: dict[int, list[ArchivedMessagePart]] = {}
        for part in part_rows:
            parts_by_event.setdefault(part.event_id, []).append(
                ArchivedMessagePart(
                    sequence=part.sequence,
                    content_type=part.content_type,
                    text=part.text,
                    raw_json=part.raw_json,
                )
            )
        events = tuple(
            ArchivedEvent(
                sequence=event.sequence,
                external_id=event.external_id,
                parent_external_id=event.parent_external_id,
                event_type=event.event_type,
                event_subtype=event.event_subtype,
                role=event.role,
                visibility=event.visibility,
                occurred_at=event.occurred_at,
                text=event.searchable_text,
                raw_json=event.raw_json,
                parse_error=event.parse_error,
                parts=tuple(parts_by_event.get(event.id, [])),
            )
            for event in event_rows
        )
        summary = ConversationSummary(
            id=row.id,
            location_id=row.location_id,
            provider=row.provider,
            hostname=row.hostname,
            external_id=row.external_id,
            title=row.title,
            conversation_kind=row.conversation_kind,
            cwd=row.cwd,
            model=row.model,
            git_branch=row.git_branch,
            started_at=row.started_at,
            ended_at=row.ended_at,
            event_count=event_count,
            message_count=message_count,
            preview=preview,
        )
        return ConversationDetail(
            summary=summary,
            relative_path=row.relative_path,
            parent_external_id=row.parent_external_id,
            metadata=row.metadata_json,
            events=events,
        )

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
        upgrade_from: int | None = None
        try:
            with self.engine.begin() as connection:
                expected_tables = set(Base.metadata.tables)
                existing_tables = set(inspect(connection).get_table_names())
                self.initialized_new_database = not expected_tables.intersection(existing_tables)
                stored_version = None
                if "schema_info" in existing_tables:
                    stored_version = connection.execute(
                        select(SchemaInfoRow.value).where(SchemaInfoRow.key == "schema_version")
                    ).scalar_one_or_none()
                    if stored_version is not None:
                        stored_version = int(stored_version)

                sqlite_version = 0
                if self.engine.dialect.name == "sqlite":
                    sqlite_version = int(
                        connection.exec_driver_sql("PRAGMA user_version").scalar_one()
                    )
                    if sqlite_version not in {0, SCHEMA_VERSION, *SCHEMA_MIGRATIONS}:
                        _raise_incompatible_schema(sqlite_version)

                self.schema_version_before = stored_version or sqlite_version or None
                if stored_version is not None and stored_version != SCHEMA_VERSION:
                    if stored_version not in SCHEMA_MIGRATIONS:
                        _raise_incompatible_schema(stored_version)
                    if not self.auto_upgrade:
                        raise SchemaUpgradeRequiredError(stored_version)
                    upgrade_from = stored_version
                    if self.schema_lock_timeout is not None:
                        _configure_schema_lock_timeout(
                            connection,
                            self.engine.dialect.name,
                            self.schema_lock_timeout,
                        )

                while stored_version is not None and stored_version != SCHEMA_VERSION:
                    migration = SCHEMA_MIGRATIONS.get(stored_version)
                    if migration is None:
                        _raise_incompatible_schema(stored_version)
                    self._report_schema_upgrade(stored_version, migration.target_version)
                    migration.upgrade(connection, self.upgrade_progress_reporter)
                    stored_version = migration.target_version
                if stored_version is not None and stored_version != SCHEMA_VERSION:
                    _raise_incompatible_schema(stored_version)

                Base.metadata.create_all(connection)
                self._validate_schema(connection)
                current_version = connection.execute(
                    select(SchemaInfoRow.value).where(SchemaInfoRow.key == "schema_version")
                ).scalar_one_or_none()
                if current_version is not None and int(current_version) != SCHEMA_VERSION:
                    _raise_incompatible_schema(int(current_version))
                if current_version is None:
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
        except DBAPIError as error:
            if upgrade_from is not None and _is_lock_timeout(error):
                raise SchemaUpgradeBlockedError(upgrade_from) from error
            raise

    def _report_schema_upgrade(self, current_version: int, target_version: int) -> None:
        if self.upgrade_reporter is not None:
            self.upgrade_reporter(current_version, target_version)

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
    def _upsert_location(
        session: Session,
        root: Path,
        provider: str,
        hostname: str,
    ) -> LocationRow:
        root_path = str(root)
        root_path_hash = _location_hash(hostname, root_path)
        location = session.scalar(
            select(LocationRow).where(LocationRow.root_path_hash == root_path_hash)
        )
        if location is None and hostname.casefold() != LEGACY_HOSTNAME:
            legacy_hash = _location_hash(LEGACY_HOSTNAME, root_path)
            location = session.scalar(
                select(LocationRow)
                .where(LocationRow.root_path_hash == legacy_hash)
                .with_for_update()
            )
            if location is not None:
                if location.root_path != root_path or location.hostname != LEGACY_HOSTNAME:
                    raise RuntimeError(
                        "A SHA-256 collision occurred while identifying a source location."
                    )
                location.hostname = hostname
                location.root_path_hash = root_path_hash
        if location is None:
            location = LocationRow(
                provider=provider,
                hostname=hostname,
                root_path=root_path,
                root_path_hash=root_path_hash,
                display_name=root.name or root_path,
            )
            session.add(location)
            session.flush()
            return location
        if location.root_path != root_path or location.hostname.casefold() != hostname.casefold():
            raise RuntimeError("A SHA-256 collision occurred while identifying a source location.")
        location.hostname = hostname
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
            identity = (
                (existing.logical_session_id, existing.chat_sha256)
                if existing.logical_session_id is not None
                else None
            )
            conversation = None
            if identity is None:
                conversation = provider.read(path, root, transcript=transcript)
                identity = _conversation_identity(conversation)
            duplicate = _find_duplicate_identity(
                session,
                identity,
                exclude_id=existing.id,
            )
            if duplicate is not None:
                if duplicate < existing.id:
                    session.delete(existing)
                    result.duplicates += 1
                    return
                session.execute(delete(ConversationRow).where(ConversationRow.id == duplicate))
                result.duplicates += 1
            if conversation is not None:
                existing.logical_session_id = conversation.logical_session_id
                existing.chat_sha256 = conversation.chat_sha256
                existing.metadata_json = _metadata_with_identity(
                    existing.metadata_json, conversation
                )
            result.unchanged += 1
            return

        conversation = provider.read(path, root, transcript=transcript)
        duplicate = _find_duplicate_conversation(
            session,
            conversation,
            exclude_id=existing.id if existing is not None else None,
        )
        if duplicate is not None:
            if existing is not None:
                session.delete(existing)
            result.duplicates += 1
            return
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
                external_id=_portable_text(source.external_id),
                parent_external_id=_portable_text(source.parent_external_id),
                event_type=_portable_text(source.event_type),
                event_subtype=_portable_text(source.event_subtype),
                role=_portable_text(source.role),
                visibility=_portable_text(source.visibility),
                occurred_at=_portable_text(source.occurred_at),
                searchable_text=_portable_text(source.searchable_text),
                raw_json=_portable_text(source.raw_json),
                parse_error=_portable_text(source.parse_error),
            )
            for source in conversation.events
        ]
        session.add_all(rows)
        session.flush()
        parts = [
            MessagePartRow(
                event_id=row.id,
                sequence=part.sequence,
                content_type=_portable_text(part.content_type),
                text=_portable_text(part.text),
                raw_json=_portable_text(part.raw_json),
            )
            for row, source in zip(rows, conversation.events, strict=True)
            for part in source.parts
        ]
        session.add_all(parts)
        result.events += len(rows)
        result.message_parts += len(parts)


def _accumulate_upload_result(result: UploadResult, file_result: UploadResult) -> None:
    for field_name in (
        "imported",
        "updated",
        "unchanged",
        "duplicates",
        "events",
        "message_parts",
    ):
        setattr(result, field_name, getattr(result, field_name) + getattr(file_result, field_name))


def _migrate_v5_to_v6(
    connection: Connection,
    progress_reporter: SchemaUpgradeProgressReporter | None = None,
) -> None:
    """Reindex native transcripts with content-block-aware message roles."""

    conversation_ids = list(
        connection.scalars(select(ConversationRow.id).order_by(ConversationRow.id))
    )
    total = len(conversation_ids)
    if progress_reporter is not None:
        progress_reporter(0, total)

    # Clear the old unique values before applying re-derived hashes so two rows
    # whose identities swap cannot transiently violate the revision index.
    connection.execute(update(ConversationRow).values(chat_sha256=None))
    seen: set[tuple[str, str]] = set()
    for completed, conversation_id in enumerate(conversation_ids, start=1):
        row = connection.execute(
            select(
                ConversationRow.id,
                ConversationRow.relative_path,
                ConversationRow.content_sha256,
                ConversationRow.transcript_codec,
                ConversationRow.transcript,
                ConversationRow.metadata_json,
                LocationRow.root_path,
                LocationRow.provider,
            )
            .join(LocationRow, ConversationRow.location_id == LocationRow.id)
            .where(ConversationRow.id == conversation_id)
        ).one()
        if row.transcript_codec != "zlib":
            raise RuntimeError(f"Unsupported transcript codec {row.transcript_codec!r} in archive.")
        try:
            transcript = zlib.decompress(row.transcript)
        except zlib.error as error:
            raise RuntimeError(
                "A stored transcript is corrupt and cannot be decompressed."
            ) from error
        if hashlib.sha256(transcript).hexdigest() != row.content_sha256:
            raise RuntimeError("A stored transcript failed its SHA-256 integrity check.")

        root = Path(row.root_path)
        conversation = get_provider(row.provider).read(
            root / Path(row.relative_path),
            root,
            transcript=transcript,
        )
        identity = _conversation_identity(conversation)
        if identity[1] is not None and identity in seen:
            connection.execute(delete(ConversationRow).where(ConversationRow.id == row.id))
            if progress_reporter is not None:
                progress_reporter(completed, total)
            continue
        if identity[1] is not None:
            seen.add(identity)

        connection.execute(delete(EventRow).where(EventRow.conversation_id == row.id))
        _insert_migrated_events(connection, row.id, conversation)
        connection.execute(
            update(ConversationRow)
            .where(ConversationRow.id == row.id)
            .values(
                external_id=_portable_text(conversation.external_id),
                logical_session_id=_portable_text(conversation.logical_session_id),
                chat_sha256=conversation.chat_sha256,
                conversation_kind=_portable_text(conversation.kind),
                parent_external_id=_portable_text(conversation.parent_external_id),
                title=_portable_text(conversation.title),
                cwd=_portable_text(conversation.cwd),
                model=_portable_text(conversation.model),
                git_branch=_portable_text(conversation.git_branch),
                started_at=_portable_text(conversation.started_at),
                ended_at=_portable_text(conversation.ended_at),
                metadata_json=_metadata_with_identity(row.metadata_json, conversation),
            )
        )
        if progress_reporter is not None:
            progress_reporter(completed, total)

    connection.execute(
        update(SchemaInfoRow)
        .where(SchemaInfoRow.key == "schema_version")
        .values(value=str(SCHEMA_VERSION))
    )


def _insert_migrated_events(
    connection: Connection, conversation_id: int, conversation: Conversation
) -> None:
    """Insert freshly normalized events while retaining the native source JSON."""

    for source in conversation.events:
        result = connection.execute(
            EventRow.__table__.insert().values(
                conversation_id=conversation_id,
                sequence=source.sequence,
                external_id=_portable_text(source.external_id),
                parent_external_id=_portable_text(source.parent_external_id),
                event_type=_portable_text(source.event_type),
                event_subtype=_portable_text(source.event_subtype),
                role=_portable_text(source.role),
                visibility=_portable_text(source.visibility),
                occurred_at=_portable_text(source.occurred_at),
                searchable_text=_portable_text(source.searchable_text),
                raw_json=_portable_text(source.raw_json),
                parse_error=_portable_text(source.parse_error),
            )
        )
        event_id = result.inserted_primary_key[0]
        if source.parts:
            connection.execute(
                MessagePartRow.__table__.insert(),
                [
                    {
                        "event_id": event_id,
                        "sequence": part.sequence,
                        "content_type": _portable_text(part.content_type),
                        "text": _portable_text(part.text),
                        "raw_json": _portable_text(part.raw_json),
                    }
                    for part in source.parts
                ],
            )


SCHEMA_MIGRATIONS: dict[int, SchemaMigration] = {
    5: SchemaMigration(target_version=6, upgrade=_migrate_v5_to_v6),
}


def _update_conversation(
    row: ConversationRow, conversation: Conversation, source_mtime_ns: int
) -> None:
    row.external_id = _portable_text(conversation.external_id) or ""
    row.conversation_kind = _portable_text(conversation.kind) or "main"
    row.parent_external_id = _portable_text(conversation.parent_external_id)
    row.title = _portable_text(conversation.title)
    row.cwd = _portable_text(conversation.cwd)
    row.model = _portable_text(conversation.model)
    row.git_branch = _portable_text(conversation.git_branch)
    row.started_at = _portable_text(conversation.started_at)
    row.ended_at = _portable_text(conversation.ended_at)
    row.source_mtime_ns = source_mtime_ns
    row.source_size = len(conversation.transcript)
    row.content_sha256 = conversation.sha256
    row.logical_session_id = _portable_text(conversation.logical_session_id)
    row.chat_sha256 = conversation.chat_sha256
    row.transcript_codec = "zlib"
    row.transcript = zlib.compress(conversation.transcript)
    row.metadata_json = _metadata_with_identity(conversation.metadata, conversation)
    row.imported_at = datetime.now(UTC)


def _find_duplicate_conversation(
    session: Session,
    conversation: Conversation,
    *,
    exclude_id: int | None,
) -> int | None:
    return _find_duplicate_identity(
        session,
        _conversation_identity(conversation),
        exclude_id=exclude_id,
    )


def _find_duplicate_identity(
    session: Session,
    identity: tuple[str, str | None],
    *,
    exclude_id: int | None,
) -> int | None:
    if identity[1] is None:
        return None
    statement = select(ConversationRow.id).where(
        ConversationRow.logical_session_id == identity[0],
        ConversationRow.chat_sha256 == identity[1],
    )
    if exclude_id is not None:
        statement = statement.where(ConversationRow.id != exclude_id)
    return session.scalar(statement.order_by(ConversationRow.id).limit(1))


def _conversation_identity(conversation: Conversation) -> tuple[str, str | None]:
    return conversation.logical_session_id, conversation.chat_sha256


def _metadata_with_identity(metadata: dict[str, Any], conversation: Conversation) -> dict[str, Any]:
    stored = _portable_json_value(metadata)
    stored["_msync"] = {
        "chat_sha256": conversation.chat_sha256,
        "logical_session_id": _portable_text(conversation.logical_session_id),
    }
    return stored


def _portable_text(value: str | None) -> str | None:
    """Replace NUL characters that SQL text types cannot portably store."""

    return value.replace("\x00", "\N{REPLACEMENT CHARACTER}") if value is not None else None


def _portable_json_value(value: Any) -> Any:
    """Make nested metadata safe for PostgreSQL JSON while retaining its shape."""

    if isinstance(value, str):
        return _portable_text(value)
    if isinstance(value, dict):
        return {_portable_text(str(key)): _portable_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_portable_json_value(item) for item in value]
    return value


def _configure_schema_lock_timeout(
    connection: Connection, dialect_name: str, timeout_seconds: int
) -> None:
    """Bound how long an explicit schema upgrade waits for concurrent clients."""

    if dialect_name == "postgresql":
        connection.exec_driver_sql(f"SET LOCAL lock_timeout = '{timeout_seconds * 1000}ms'")
    elif dialect_name == "mysql":
        connection.exec_driver_sql(f"SET SESSION innodb_lock_wait_timeout = {timeout_seconds}")
        connection.exec_driver_sql(f"SET SESSION lock_wait_timeout = {timeout_seconds}")
    elif dialect_name == "sqlite":
        connection.exec_driver_sql(f"PRAGMA busy_timeout = {timeout_seconds * 1000}")


def _is_lock_timeout(error: DBAPIError) -> bool:
    message = str(error).casefold()
    return any(
        marker in message
        for marker in (
            "database is locked",
            "database table is locked",
            "lock timeout",
            "lock wait timeout",
        )
    )


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


def _location_hash(hostname: str, root_path: str) -> str:
    return _text_hash(f"{hostname.casefold()}\0{root_path}")


def _normalize_hostname(hostname: str | None) -> str:
    value = socket.gethostname() if hostname is None else hostname
    normalized = value.strip()
    if not normalized:
        raise ValueError("Location hostname must not be empty.")
    if len(normalized) > 255:
        raise ValueError("Location hostname must not exceed 255 characters.")
    return normalized


def _raise_incompatible_schema(version: int) -> None:
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {version} is newer than this msync schema "
            f"({SCHEMA_VERSION}); upgrade msync before opening the database."
        )
    raise RuntimeError(
        f"Database schema version {version} has no supported upgrade path to "
        f"{SCHEMA_VERSION}; export it with a compatible msync release or create a new database."
    )
