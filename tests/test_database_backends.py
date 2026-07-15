from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from msync.database import Archive, SchemaUpgradeRequiredError, _normalize_database
from msync.providers import get_provider
from msync.tables import Base


def test_new_database_is_initialized_and_validated(tmp_path: Path) -> None:
    database = tmp_path / "new/archive.sqlite"

    with Archive(database) as archive:
        assert archive.initialized_new_database is True

    with closing(sqlite3.connect(database)) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        triggers = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
        assert set(Base.metadata.tables) <= tables
        assert "events_fts" in tables
        assert {
            "events_fts_insert",
            "events_fts_delete",
            "events_fts_update",
        } <= triggers
        assert connection.execute(
            "SELECT value FROM schema_info WHERE key = 'schema_version'"
        ).fetchone() == ("6",)
        primary_keys = {
            table: tuple(
                row[1] for row in connection.execute(f"PRAGMA table_info({table})") if row[5]
            )
            for table in Base.metadata.tables
        }
        assert primary_keys == {
            "schema_info": ("key",),
            "locations": ("id",),
            "conversations": ("id",),
            "events": ("id",),
            "message_parts": ("id",),
        }

    with Archive(database) as archive:
        assert archive.initialized_new_database is False


def test_schema_older_than_v5_has_no_supported_upgrade_path(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    with Archive(database):
        pass

    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE schema_info SET value = '4' WHERE key = 'schema_version'")
        connection.execute("PRAGMA user_version = 4")
        connection.commit()

    with pytest.raises(RuntimeError, match="no supported upgrade path"):
        Archive(database)


def test_v5_archive_reindexes_tool_results_from_lossless_transcript(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    root = (tmp_path / ".claude").resolve()
    transcript = root / "projects" / "-work" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    records = [
        {
            "type": "user",
            "uuid": "user-1",
            "sessionId": "session-1",
            "message": {"role": "user", "content": "Inspect the log"},
        },
        {
            "type": "user",
            "uuid": "tool-result-1",
            "sessionId": "session-1",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "command output",
                    }
                ],
            },
        },
    ]
    source = "".join(json.dumps(record) + "\n" for record in records).encode()
    transcript.write_bytes(source)
    with Archive(database) as archive:
        archive.upload(
            root=root,
            provider=get_provider("claude"),
            transcripts=[transcript],
        )

    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE events SET role = 'user', event_subtype = NULL WHERE sequence = 1"
        )
        connection.execute("UPDATE conversations SET title = 'command output'")
        connection.execute("UPDATE schema_info SET value = '5' WHERE key = 'schema_version'")
        connection.execute("PRAGMA user_version = 5")
        connection.commit()

    upgrade_steps: list[tuple[int, int]] = []
    with Archive(database, upgrade_reporter=lambda *step: upgrade_steps.append(step)) as archive:
        summary = archive.browse_conversations()[0]
        detail = archive.browse_conversation(summary.id)
        stored = archive.conversations()[0].conversation.transcript
        matches = archive.search("command output")

    assert detail is not None
    assert summary.title == "Inspect the log"
    assert summary.message_count == 1
    assert [event.role for event in detail.events] == ["user", "tool"]
    assert detail.events[1].event_subtype == "tool_result"
    assert [match.role for match in matches] == ["tool"]
    assert stored == source
    assert upgrade_steps == [(5, 6)]
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)
        assert connection.execute(
            "SELECT value FROM schema_info WHERE key = 'schema_version'"
        ).fetchone() == ("6",)


def test_old_archive_can_require_explicit_schema_upgrade(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    with Archive(database):
        pass

    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE schema_info SET value = '5' WHERE key = 'schema_version'")
        connection.execute("PRAGMA user_version = 5")
        connection.commit()

    with pytest.raises(SchemaUpgradeRequiredError, match="msync upgrade") as captured:
        Archive(database, auto_upgrade=False)

    assert captured.value.current_version == 5
    assert captured.value.target_version == 6


def test_incompatible_existing_schema_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "broken.sqlite"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE locations (id INTEGER PRIMARY KEY)")
        connection.commit()

    with pytest.raises(RuntimeError, match="locations missing columns"):
        Archive(database)


def test_schema_compiles_for_postgresql_and_mysql() -> None:
    postgresql_ddl = _compiled_schema(postgresql.dialect())
    mysql_ddl = _compiled_schema(mysql.dialect())

    assert "BYTEA" in postgresql_ddl
    assert "JSON" in postgresql_ddl
    assert "LONGBLOB" in mysql_ddl
    assert "LONGTEXT" in mysql_ddl
    assert "conversations_location_path_hash_uq" in mysql_ddl
    assert "conversations_logical_revision_uq" in postgresql_ddl
    assert "conversations_logical_revision_uq" in mysql_ddl


def test_common_database_urls_select_supported_drivers() -> None:
    postgres_url, postgres_path = _normalize_database(
        "postgresql://alice:secret@database.example/msync"
    )
    mysql_url, mysql_path = _normalize_database("mysql://alice:secret@database.example/msync")

    assert postgres_url.drivername == "postgresql+psycopg"
    assert mysql_url.drivername == "mysql+pymysql"
    assert postgres_path is None
    assert mysql_path is None
    assert "secret" not in postgres_url.render_as_string(hide_password=True)


def test_old_schema_version_is_rejected_without_migration(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    with Archive(database) as archive:
        assert archive.initialized_new_database

    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA user_version = 1")
        connection.commit()

    with pytest.raises(RuntimeError, match="create a new database"):
        Archive(database)


def test_newer_schema_version_requires_newer_msync(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    with Archive(database):
        pass

    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE schema_info SET value = '7' WHERE key = 'schema_version'")
        connection.execute("PRAGMA user_version = 7")
        connection.commit()

    with pytest.raises(RuntimeError, match="upgrade msync"):
        Archive(database)


def _compiled_schema(dialect: object) -> str:
    statements: list[str] = []
    for table in Base.metadata.sorted_tables:
        statements.append(str(CreateTable(table).compile(dialect=dialect)))
        statements.extend(
            str(CreateIndex(index).compile(dialect=dialect)) for index in table.indexes
        )
    return "\n".join(statements)
