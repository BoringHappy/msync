from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from msync.cli import app
from msync.database import Archive


def _write_codex_transcript(root: Path, *, session_id: str = "cli-session") -> Path:
    transcript = root / "sessions/rollout.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-14T12:00:00Z",
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": "/tmp"},
            }
        )
        + "\n"
    )
    return transcript


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


def test_upload_sends_native_transcripts_to_remote_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / ".codex-remote"
    transcript = _write_codex_transcript(root, session_id="remote-session")
    captured: dict[str, Any] = {}

    def fake_post(
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: int,
    ) -> httpx.Response:
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return httpx.Response(
            200,
            json={
                "location_id": 12,
                "scanned": 1,
                "imported": 1,
                "updated": 0,
                "unchanged": 0,
                "duplicates": 0,
                "events": 1,
                "message_parts": 0,
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("msync.cli.httpx.post", fake_post)
    result = CliRunner().invoke(
        app,
        [
            "upload",
            "--dir",
            str(root),
            "--url",
            "https://history.example/msync/",
            "--token",
            "alice-token",
            "--hostname",
            "alice-laptop",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Upload complete" in result.output
    assert "Server" in result.output
    assert "https://history.example/msync" in result.output
    assert "alice-token" not in result.output
    assert captured["url"] == "https://history.example/msync/api/upload"
    assert captured["headers"] == {"Authorization": "Bearer alice-token"}
    assert captured["timeout"] == 120
    payload = captured["json"]
    assert payload["provider"] == "codex"
    assert payload["hostname"] == "alice-laptop"
    assert payload["root_path"] == str(root.resolve())
    assert payload["transcripts"][0]["relative_path"] == "sessions/rollout.jsonl"
    assert payload["transcripts"][0]["content_base64"]
    assert transcript.read_bytes()


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--url", "https://history.example"], "--url requires --token"),
        (["--token", "secret"], "--token requires --url"),
        (
            [
                "--url",
                "https://history.example",
                "--token",
                "secret",
                "--database",
                "archive.sqlite",
            ],
            "--url and --database cannot be used together",
        ),
    ],
)
def test_upload_rejects_conflicting_remote_options(
    tmp_path: Path,
    arguments: list[str],
    message: str,
) -> None:
    root = tmp_path / ".codex"
    _write_codex_transcript(root)

    result = CliRunner().invoke(app, ["upload", "--dir", str(root), *arguments])

    assert result.exit_code == 1
    assert message in result.output


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
    assert "Upgrading database schema 6 → 7" in result.output
    assert "Database schema upgrade complete: 5 → 7" in result.output
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT value FROM schema_info WHERE key = 'schema_version'"
        ).fetchone() == ("7",)


def test_upgrade_command_reports_current_schema(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    with Archive(database):
        pass

    result = CliRunner().invoke(app, ["upgrade", "--database", str(database)])

    assert result.exit_code == 0, result.output
    assert "Database schema is current at version 7" in result.output


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
    assert "must be upgraded to 7" in result.output
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
