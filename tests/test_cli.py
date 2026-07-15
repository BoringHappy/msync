from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from typer.testing import CliRunner

from msync.cli import app
from msync.database import Archive


def test_upload_command(tmp_path: Path) -> None:
    root = tmp_path / ".codex_custom"
    transcript = root / "sessions/rollout.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-14T12:00:00Z",
                "type": "session_meta",
                "payload": {"id": "cli-session", "cwd": "/tmp"},
            }
        )
        + "\n"
    )
    database = tmp_path / "data/msync.sqlite"

    result = CliRunner().invoke(
        app,
        [
            "upload",
            "--dir",
            str(root),
            "--database",
            str(database),
            "--hostname",
            "cli-workstation",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Upload complete" in result.output
    assert "codex" in result.output
    assert "cli-workstation" in result.output
    assert database.is_file()
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT count(*) FROM conversations").fetchone() == (1,)
        assert connection.execute("SELECT hostname FROM locations").fetchone() == (
            "cli-workstation",
        )


def test_upload_rejects_empty_directory(tmp_path: Path) -> None:
    root = tmp_path / ".codex_empty"
    root.mkdir()

    result = CliRunner().invoke(app, ["upload", "--dir", str(root)])

    assert result.exit_code == 1
    assert "No conversation transcripts found" in result.output


def test_upload_rejects_empty_hostname(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    transcript = root / "sessions/rollout.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-14T12:00:00Z",
                "type": "session_meta",
                "payload": {"id": "cli-session", "cwd": "/tmp"},
            }
        )
        + "\n"
    )

    result = CliRunner().invoke(app, ["upload", "--dir", str(root), "--hostname", " "])

    assert result.exit_code == 1
    assert "Location hostname must not be empty" in result.output


def test_upload_accepts_explicit_claude_provider(tmp_path: Path) -> None:
    root = tmp_path / "custom-history"
    transcript = root / "projects/-work/session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "message-1",
                "sessionId": "claude-session",
                "timestamp": "2026-07-14T12:00:00Z",
                "message": {"role": "user", "content": "hello Claude"},
            }
        )
        + "\n"
    )
    database = tmp_path / "claude.sqlite"

    result = CliRunner().invoke(
        app,
        [
            "upload",
            "--dir",
            str(root),
            "--provider",
            "claude",
            "--database",
            str(database),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "claude" in result.output
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT provider FROM locations").fetchone() == ("claude",)


def test_upgrade_command_migrates_archive_and_reports_steps(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    with Archive(database):
        pass
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE schema_info SET value = '5' WHERE key = 'schema_version'")
        connection.execute("PRAGMA user_version = 5")
        connection.commit()

    result = CliRunner().invoke(
        app,
        ["upgrade", "--database", str(database), "--lock-timeout", "1"],
    )

    assert result.exit_code == 0, result.output
    assert "Upgrading database schema 5 → 6" in result.output
    assert "Database schema upgrade complete: 5 → 6" in result.output
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT value FROM schema_info WHERE key = 'schema_version'"
        ).fetchone() == ("6",)


def test_upgrade_command_reports_current_schema(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    with Archive(database):
        pass

    result = CliRunner().invoke(app, ["upgrade", "--database", str(database)])

    assert result.exit_code == 0, result.output
    assert "Database schema is current at version 6" in result.output


def test_upgrade_command_reports_concurrent_transaction(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    with Archive(database):
        pass
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE schema_info SET value = '5' WHERE key = 'schema_version'")
        connection.execute("PRAGMA user_version = 5")
        connection.commit()

    with closing(sqlite3.connect(database)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        result = CliRunner().invoke(
            app,
            ["upgrade", "--database", str(database), "--lock-timeout", "1"],
        )
        blocker.rollback()

    assert result.exit_code == 1
    assert "blocked by another" in result.output
    assert "transaction." in result.output
    assert "Stop or finish other msync uploads" in result.output


def test_regular_command_requires_explicit_schema_upgrade(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    with Archive(database):
        pass
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE schema_info SET value = '5' WHERE key = 'schema_version'")
        connection.execute("PRAGMA user_version = 5")
        connection.commit()

    result = CliRunner().invoke(app, ["sample", "1", "--database", str(database)])

    assert result.exit_code == 1
    assert "must be upgraded to 6" in result.output
    assert "msync upgrade --database <database>" in result.output


def test_search_command_finds_text_with_like_query(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    transcript = root / "sessions/rollout.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-07-14T12:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "search-session", "cwd": "/tmp"},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-07-14T12:00:01Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "Find the blue widget today",
                        },
                    }
                ),
            ]
        )
        + "\n"
    )
    database = tmp_path / "search.sqlite"
    upload_result = CliRunner().invoke(
        app,
        ["upload", "--dir", str(root), "--database", str(database)],
    )
    assert upload_result.exit_code == 0, upload_result.output

    result = CliRunner().invoke(
        app,
        ["search", "blue widget", "--database", str(database)],
    )

    assert result.exit_code == 0, result.output
    assert "Search results (1)" in result.output
    assert "codex" in result.output
    assert "Find the blue widget today" in result.output


def test_search_command_reports_no_matches(tmp_path: Path) -> None:
    database = tmp_path / "empty.sqlite"

    result = CliRunner().invoke(
        app,
        ["search", "missing", "--database", str(database)],
    )

    assert result.exit_code == 0, result.output
    assert "No matches found for missing." in result.output


def test_sample_command_limits_random_messages(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    transcript = root / "sessions/rollout.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-07-14T12:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "sample-session", "cwd": "/tmp"},
                    }
                )
            ]
            + [
                json.dumps(
                    {
                        "timestamp": f"2026-07-14T12:00:0{sequence}Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": f"Sample message {sequence}",
                        },
                    }
                )
                for sequence in range(1, 4)
            ]
        )
        + "\n"
    )
    database = tmp_path / "sample.sqlite"
    upload_result = CliRunner().invoke(
        app,
        ["upload", "--dir", str(root), "--database", str(database)],
    )
    assert upload_result.exit_code == 0, upload_result.output

    result = CliRunner().invoke(
        app,
        ["sample", "2", "--database", str(database)],
    )

    assert result.exit_code == 0, result.output
    assert "Samples (2)" in result.output
    assert " 1  codex " in result.output
    assert " 2  codex " in result.output
    assert result.output.count("─") > 2
    assert result.output.count("Conversation") == 2
    assert result.output.count("Role         user") == 2


def test_sample_command_reports_empty_archive(tmp_path: Path) -> None:
    database = tmp_path / "empty.sqlite"

    result = CliRunner().invoke(
        app,
        ["sample", "3", "--database", str(database)],
    )

    assert result.exit_code == 0, result.output
    assert "No archived messages found." in result.output
