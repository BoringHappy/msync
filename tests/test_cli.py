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
