from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from typer.testing import CliRunner

from msync.cli import app
from msync.database import Archive
from msync.providers import get_provider
from msync.synchronization import MANIFEST_NAME
from msync.tables import LocationRow

POSTGRES_URL = os.environ.get("MSYNC_POSTGRES_URL")


@pytest.mark.skipif(not POSTGRES_URL, reason="MSYNC_POSTGRES_URL is not configured")
def test_postgres_sync_writes_round_trip_native_histories(tmp_path: Path) -> None:
    claude_root = tmp_path / ".claude-integration"
    codex_root = tmp_path / ".codex-integration"
    claude_path = claude_root / "projects/-tmp-project/claude-pg-session.jsonl"
    codex_path = codex_root / "sessions/2026/07/14/rollout-pg-session.jsonl"
    _write_jsonl(
        claude_path,
        [
            {
                "type": "user",
                "uuid": "claude-pg-message",
                "sessionId": "claude-pg-session",
                "timestamp": "2026-07-14T10:00:00Z",
                "cwd": "/tmp/project",
                "message": {"role": "user", "content": "Postgres Claude prompt"},
            }
        ],
    )
    _write_jsonl(
        codex_path,
        [
            {
                "timestamp": "2026-07-14T11:00:00Z",
                "type": "session_meta",
                "payload": {"id": "019f61a0-0000-7000-8000-000000000099", "cwd": "/tmp"},
            },
            {
                "timestamp": "2026-07-14T11:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Postgres Codex prompt"},
            },
        ],
    )
    roots = [claude_root.resolve(), codex_root.resolve()]

    try:
        result = CliRunner().invoke(
            app,
            [
                "sync",
                "--dir",
                str(claude_root),
                "--dir",
                str(codex_root),
                "--database",
                POSTGRES_URL,
            ],
        )

        assert result.exit_code == 0, result.output
        claude_generated = _generated_paths(claude_root)
        codex_generated = _generated_paths(codex_root)
        assert len(claude_generated) == 1
        assert len(codex_generated) == 1
        assert (
            get_provider("claude").read(claude_generated[0], claude_root).title
            == "Postgres Codex prompt"
        )
        assert (
            get_provider("codex").read(codex_generated[0], codex_root).title
            == "Postgres Claude prompt"
        )
        with Archive(POSTGRES_URL) as archive, archive.engine.connect() as connection:
            count = connection.execute(
                select(LocationRow.id).where(LocationRow.root_path.in_(str(root) for root in roots))
            ).all()
        assert len(count) == 2
    finally:
        with Archive(POSTGRES_URL) as archive, archive.engine.begin() as connection:
            connection.execute(
                delete(LocationRow).where(LocationRow.root_path.in_(str(root) for root in roots))
            )


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def _generated_paths(root: Path) -> list[Path]:
    manifest = json.loads((root / MANIFEST_NAME).read_text())
    return [root / relative_path for relative_path in manifest["files"]]
