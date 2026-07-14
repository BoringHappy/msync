from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from typer.testing import CliRunner

from msync.cli import app


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
        ["upload", "--dir", str(root), "--database", str(database)],
    )

    assert result.exit_code == 0, result.output
    assert "Upload complete" in result.output
    assert "codex" in result.output
    assert database.is_file()
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT count(*) FROM conversations").fetchone() == (1,)


def test_upload_rejects_empty_directory(tmp_path: Path) -> None:
    root = tmp_path / ".codex_empty"
    root.mkdir()

    result = CliRunner().invoke(app, ["upload", "--dir", str(root)])

    assert result.exit_code == 1
    assert "No conversation transcripts found" in result.output


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
    assert "1. codex" in result.output
    assert "2. codex" in result.output
    assert "3. codex" not in result.output


def test_sample_command_reports_empty_archive(tmp_path: Path) -> None:
    database = tmp_path / "empty.sqlite"

    result = CliRunner().invoke(
        app,
        ["sample", "3", "--database", str(database)],
    )

    assert result.exit_code == 0, result.output
    assert "No archived messages found." in result.output
