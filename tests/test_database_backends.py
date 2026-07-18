from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql
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
        ).fetchone() == ("9",)
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
            "conversation_metrics": ("conversation_id",),
            "archive_revisions": ("account_username",),
            "upload_history": ("id",),
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
                        "content": "command\x00output",
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
        matches = archive.search("command")

    assert detail is not None
    assert summary.title == "Inspect the log"
    assert summary.message_count == 1
    assert [event.role for event in detail.events] == ["user", "tool"]
    assert detail.events[1].event_subtype == "tool_result"
    assert detail.events[1].text == "command\N{REPLACEMENT CHARACTER}output"
    assert [match.role for match in matches] == ["tool"]
    assert stored == source
    assert upgrade_steps == [(5, 6), (6, 7), (7, 8), (8, 9)]
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (9,)
        assert connection.execute(
            "SELECT value FROM schema_info WHERE key = 'schema_version'"
        ).fetchone() == ("9",)


def test_v6_archive_migrates_tenant_columns_and_revision_index(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    root = (tmp_path / ".codex").resolve()
    transcript = root / "sessions" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-14T12:00:00Z",
                "type": "session_meta",
                "payload": {"id": "migration-session", "cwd": "/tmp"},
            }
        )
        + "\n"
    )
    with Archive(database) as archive:
        archive.upload(root=root, provider=get_provider("codex"), transcripts=[transcript])

    legacy_hash = hashlib.sha256(f"{archive.hostname.casefold()}\0{root}".encode()).hexdigest()
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("DROP INDEX conversations_logical_revision_uq")
        connection.execute(
            "CREATE UNIQUE INDEX conversations_logical_revision_uq "
            "ON conversations(logical_session_id, chat_sha256)"
        )
        connection.execute("ALTER TABLE conversations DROP COLUMN account_username")
        connection.execute("ALTER TABLE locations DROP COLUMN account_username")
        connection.execute("UPDATE locations SET root_path_hash = ?", (legacy_hash,))
        connection.execute("UPDATE schema_info SET value = '6' WHERE key = 'schema_version'")
        connection.execute("PRAGMA user_version = 6")
        connection.commit()

    upgrade_steps: list[tuple[int, int]] = []
    with Archive(database, upgrade_reporter=lambda *step: upgrade_steps.append(step)) as archive:
        assert archive.browse_conversations()[0].external_id == "migration-session"

    expected_hash = hashlib.sha256(
        f"\0{archive.hostname.casefold()}\0{root}".encode()
    ).hexdigest()
    with closing(sqlite3.connect(database)) as connection:
        location_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(locations)")
        }
        conversation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(conversations)")
        }
        revision_columns = tuple(
            row[2]
            for row in connection.execute(
                "PRAGMA index_info(conversations_logical_revision_uq)"
            )
        )
        stored_location = connection.execute(
            "SELECT account_username, root_path_hash FROM locations"
        ).fetchone()

    assert upgrade_steps == [(6, 7), (7, 8), (8, 9)]
    assert "account_username" in location_columns
    assert "account_username" in conversation_columns
    assert revision_columns == ("account_username", "logical_session_id", "chat_sha256")
    assert stored_location == ("", expected_hash)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT event_count, message_count FROM conversation_metrics"
        ).fetchone() == (1, 0)
        assert connection.execute(
            "SELECT account_username, revision FROM archive_revisions"
        ).fetchone() == ("", 1)


def test_v8_archive_adds_upload_history_and_backfills_token_metrics(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    root = (tmp_path / ".codex").resolve()
    transcript = root / "sessions/token-migration.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "".join(
            json.dumps(record) + "\n"
            for record in (
                {
                    "timestamp": "2026-07-14T12:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "token-migration", "cwd": "/tmp"},
                },
                {
                    "timestamp": "2026-07-14T12:00:01Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 80,
                                "cached_input_tokens": 30,
                                "output_tokens": 20,
                                "total_tokens": 100,
                            }
                        },
                    },
                },
            )
        )
    )
    with Archive(database) as archive:
        archive.upload(root=root, provider=get_provider("codex"), transcripts=[transcript])

    with closing(sqlite3.connect(database)) as connection:
        connection.execute("DROP TABLE upload_history")
        connection.execute("ALTER TABLE conversation_metrics DROP COLUMN input_token_count")
        connection.execute("ALTER TABLE conversation_metrics DROP COLUMN output_token_count")
        connection.execute("ALTER TABLE conversation_metrics DROP COLUMN cached_input_token_count")
        connection.execute("UPDATE schema_info SET value = '8' WHERE key = 'schema_version'")
        connection.execute("PRAGMA user_version = 8")
        connection.commit()

    upgrade_steps: list[tuple[int, int]] = []
    with Archive(database, upgrade_reporter=lambda *step: upgrade_steps.append(step)) as archive:
        totals = archive.browse_metrics().totals

    assert upgrade_steps == [(8, 9)]
    assert totals.input_tokens == 80
    assert totals.output_tokens == 20
    assert totals.cached_input_tokens == 30
    assert totals.tokens == 100
    with closing(sqlite3.connect(database)) as connection:
        assert {row[1] for row in connection.execute("PRAGMA table_info(upload_history)")} >= {
            "id",
            "account_username",
            "relative_path",
            "uploaded_at",
        }


def test_old_archive_can_require_explicit_schema_upgrade(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    with Archive(database):
        pass

    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE schema_info SET value = '5' WHERE key = 'schema_version'")
        connection.execute("PRAGMA user_version = 5")
        connection.commit()

    with pytest.raises(SchemaUpgradeRequiredError, match="msync server") as captured:
        Archive(database, auto_upgrade=False)

    assert captured.value.current_version == 5
    assert captured.value.target_version == 9


def test_incompatible_existing_schema_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "broken.sqlite"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE locations (id INTEGER PRIMARY KEY)")
        connection.commit()

    with pytest.raises(RuntimeError, match="locations missing columns"):
        Archive(database)


def test_schema_compiles_for_postgresql() -> None:
    postgresql_ddl = _compiled_schema(postgresql.dialect())

    assert "BYTEA" in postgresql_ddl
    assert "JSON" in postgresql_ddl
    assert "conversations_logical_revision_uq" in postgresql_ddl


def test_common_database_urls_select_supported_drivers() -> None:
    postgres_url, postgres_path = _normalize_database(
        "postgresql://alice:secret@database.example/msync"
    )
    assert postgres_url.drivername == "postgresql+psycopg"
    assert postgres_path is None
    assert "secret" not in postgres_url.render_as_string(hide_password=True)


def test_mysql_database_urls_are_rejected() -> None:
    with pytest.raises(ValueError, match="use SQLite or PostgreSQL"):
        _normalize_database("mysql://alice:secret@database.example/msync")


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
        connection.execute("UPDATE schema_info SET value = '10' WHERE key = 'schema_version'")
        connection.execute("PRAGMA user_version = 10")
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
