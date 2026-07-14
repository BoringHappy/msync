from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from msync.database import Archive, _normalize_database
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
        ).fetchone() == ("3",)

    with Archive(database) as archive:
        assert archive.initialized_new_database is False


def test_incompatible_existing_schema_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "broken.sqlite"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE locations (id INTEGER PRIMARY KEY)")
        connection.execute("PRAGMA user_version = 3")
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


def _compiled_schema(dialect: object) -> str:
    statements: list[str] = []
    for table in Base.metadata.sorted_tables:
        statements.append(str(CreateTable(table).compile(dialect=dialect)))
        statements.extend(
            str(CreateIndex(index).compile(dialect=dialect)) for index in table.indexes
        )
    return "\n".join(statements)
