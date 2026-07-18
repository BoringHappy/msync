from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from msync import database as database_module
from msync.cli import app
from msync.database import Archive
from msync.providers import get_provider
from msync.synchronization import MANIFEST_NAME
from msync.tables import ConversationRow, LocationRow

POSTGRES_URL = os.environ.get("MSYNC_POSTGRES_URL")


@pytest.fixture
def postgres_url() -> Iterator[str]:
    """Give each integration test an isolated schema in the configured database."""

    if POSTGRES_URL is None:
        pytest.skip("MSYNC_POSTGRES_URL is not configured")
    schema = f"msync_test_{uuid4().hex}"
    admin_url = make_url(POSTGRES_URL)
    if admin_url.drivername in {"postgres", "postgresql"}:
        admin_url = admin_url.set(drivername="postgresql+psycopg")
    engine = create_engine(admin_url, pool_pre_ping=True)
    created = False
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        created = True
        isolated_url = admin_url.update_query_dict({"options": f"-csearch_path={schema}"})
        yield isolated_url.render_as_string(hide_password=False)
    finally:
        if created:
            with engine.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        engine.dispose()


@pytest.mark.skipif(not POSTGRES_URL, reason="MSYNC_POSTGRES_URL is not configured")
def test_postgres_sync_writes_round_trip_native_histories(
    tmp_path: Path, postgres_url: str
) -> None:
    run_id = str(uuid4())
    claude_prompt = f"Postgres Claude prompt {run_id}"
    codex_prompt = f"Postgres Codex prompt {run_id}"
    claude_root = tmp_path / ".claude-integration"
    codex_root = tmp_path / ".codex-integration"
    claude_path = claude_root / f"projects/-tmp-project/{run_id}.jsonl"
    codex_path = codex_root / f"sessions/2026/07/14/rollout-{run_id}.jsonl"
    _write_jsonl(
        claude_path,
        [
            {
                "type": "user",
                "uuid": "claude-pg-message",
                "sessionId": run_id,
                "timestamp": "2026-07-14T10:00:00Z",
                "cwd": "/tmp/project",
                "message": {"role": "user", "content": claude_prompt},
            }
        ],
    )
    _write_jsonl(
        codex_path,
        [
            {
                "timestamp": "2026-07-14T11:00:00Z",
                "type": "session_meta",
                "payload": {"id": run_id, "cwd": "/tmp"},
            },
            {
                "timestamp": "2026-07-14T11:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": codex_prompt},
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
                postgres_url,
            ],
        )

        assert result.exit_code == 0, result.output
        claude_generated = _generated_paths(claude_root)
        codex_generated = _generated_paths(codex_root)
        assert len(claude_generated) == 1
        assert len(codex_generated) == 1
        assert get_provider("claude").read(claude_generated[0], claude_root).title == codex_prompt
        assert get_provider("codex").read(codex_generated[0], codex_root).title == claude_prompt

        for root, provider in ((claude_root, "claude"), (codex_root, "codex")):
            feedback = CliRunner().invoke(
                app,
                [
                    "upload",
                    "--dir",
                    str(root),
                    "--provider",
                    provider,
                    "--database",
                    postgres_url,
                ],
            )
            assert feedback.exit_code == 0, feedback.output
            assert re.search(r"Duplicates skipped\s+1", feedback.output)

        repeated = CliRunner().invoke(
            app,
            [
                "sync",
                "--dir",
                str(claude_root),
                "--dir",
                str(codex_root),
                "--database",
                postgres_url,
            ],
        )
        assert repeated.exit_code == 0, repeated.output
        assert len(_generated_paths(claude_root)) == 1
        assert len(_generated_paths(codex_root)) == 1

        with Archive(postgres_url) as archive, archive.engine.connect() as connection:
            locations = connection.execute(
                select(LocationRow.id).where(LocationRow.root_path.in_(str(root) for root in roots))
            ).all()
            conversations = connection.execute(
                select(ConversationRow.id)
                .join(LocationRow, ConversationRow.location_id == LocationRow.id)
                .where(LocationRow.root_path.in_(str(root) for root in roots))
            ).all()
        assert len(locations) == 2
        assert len(conversations) == 2
    finally:
        with Archive(postgres_url) as archive, archive.engine.begin() as connection:
            connection.execute(
                delete(LocationRow).where(LocationRow.root_path.in_(str(root) for root in roots))
            )


@pytest.mark.skipif(not POSTGRES_URL, reason="MSYNC_POSTGRES_URL is not configured")
def test_postgres_concurrent_uploads_keep_one_logical_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, postgres_url: str
) -> None:
    session_id = str(uuid4())
    items: list[tuple[Path, Path]] = []
    for name in ("a", "b"):
        root = (tmp_path / f"codex-{name}").resolve()
        path = root / "sessions" / f"{name}.jsonl"
        _write_jsonl(
            path,
            [
                {
                    "timestamp": "2026-07-14T10:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": session_id, "cwd": "/tmp"},
                },
                {
                    "timestamp": "2026-07-14T10:00:01Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "Concurrent PostgreSQL revision",
                    },
                },
            ],
        )
        items.append((root, path.resolve()))
    roots = [str(root) for root, _ in items]
    barrier = threading.Barrier(2)
    call_lock = threading.Lock()
    calls = 0
    original_find = database_module._find_duplicate_identity

    def synchronized_find(*args: object, **kwargs: object) -> int | None:
        nonlocal calls
        result = original_find(*args, **kwargs)
        with call_lock:
            calls += 1
            should_wait = calls <= 2
        if should_wait:
            barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(database_module, "_find_duplicate_identity", synchronized_find)
    errors: list[BaseException] = []

    def upload(item: tuple[Path, Path]) -> None:
        root, path = item
        try:
            with Archive(postgres_url) as archive:
                archive.upload(
                    root=root,
                    provider=get_provider("codex"),
                    transcripts=[path],
                )
        except BaseException as error:
            errors.append(error)

    try:
        with Archive(postgres_url):
            pass
        threads = [threading.Thread(target=upload, args=(item,)) for item in items]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        with Archive(postgres_url) as archive, archive.engine.connect() as connection:
            count = connection.scalar(
                select(func.count(ConversationRow.id))
                .join(LocationRow, ConversationRow.location_id == LocationRow.id)
                .where(LocationRow.root_path.in_(roots))
            )
        assert count == 1
    finally:
        with Archive(postgres_url) as archive, archive.engine.begin() as connection:
            connection.execute(delete(LocationRow).where(LocationRow.root_path.in_(roots)))


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def _generated_paths(root: Path) -> list[Path]:
    manifest = json.loads((root / MANIFEST_NAME).read_text())
    return [root / relative_path for relative_path in manifest["files"]]
