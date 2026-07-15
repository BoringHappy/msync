from __future__ import annotations

import json
import sqlite3
import zlib
from contextlib import closing
from pathlib import Path

from msync.database import Archive
from msync.providers import detect_provider, get_provider


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records)
    path.write_text(content)
    return content.encode()


def _codex_records(session_id: str = "session-1") -> list[dict[str, object]]:
    return [
        {
            "timestamp": "2026-07-14T10:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "cwd": "/work/project",
                "cli_version": "1.2.3",
                "model_provider": "openai",
                "git": {"branch": "main", "commit_hash": "abc123"},
            },
        },
        {
            "timestamp": "2026-07-14T10:00:01Z",
            "type": "turn_context",
            "payload": {"model": "gpt-test"},
        },
        {
            "timestamp": "2026-07-14T10:00:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Find the blue widget"},
        },
        {
            "timestamp": "2026-07-14T10:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "I found it."}],
            },
        },
    ]


def test_codex_upload_is_lossless_searchable_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    transcript_path = root / "sessions/2026/07/14/rollout-session-1.jsonl"
    original = _write_jsonl(transcript_path, _codex_records())
    database = tmp_path / "archive.sqlite"

    provider = detect_provider(root)
    paths = provider.discover(root)
    with Archive(database) as archive:
        first = archive.upload(root=root, provider=provider, transcripts=paths)
        second = archive.upload(root=root, provider=provider, transcripts=paths)

    assert provider.name == "codex"
    assert first.imported == 1
    assert first.events == 4
    assert second.imported == 0
    assert second.unchanged == 1

    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            """
            SELECT external_id, title, cwd, model, git_branch, source_size, transcript
            FROM conversations
            """
        ).fetchone()
        assert row is not None
        assert row[:6] == (
            "session-1",
            "Find the blue widget",
            "/work/project",
            "gpt-test",
            "main",
            len(original),
        )
        assert zlib.decompress(row[6]) == original
        match = connection.execute(
            "SELECT searchable_text FROM events_fts WHERE events_fts MATCH 'widget'"
        ).fetchone()
        assert match == ("Find the blue widget",)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_changed_transcript_replaces_normalized_events(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    path = root / "sessions/rollout.jsonl"
    _write_jsonl(path, _codex_records())
    database = tmp_path / "archive.sqlite"
    provider = get_provider("codex")
    with Archive(database) as archive:
        archive.upload(root=root, provider=provider, transcripts=[path])
        records = _codex_records()
        records[-1]["payload"] = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Updated answer"}],
        }
        _write_jsonl(path, records)
        result = archive.upload(root=root, provider=provider, transcripts=[path])

    assert result.updated == 1
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT count(*) FROM conversations").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM events").fetchone() == (4,)
        assert connection.execute(
            "SELECT searchable_text FROM events_fts WHERE events_fts MATCH 'updated'"
        ).fetchone() == ("Updated answer",)


def test_same_logical_revision_from_two_locations_is_deduplicated(tmp_path: Path) -> None:
    first_root = tmp_path / ".codex"
    second_root = tmp_path / ".codex_another"
    first_path = first_root / "sessions/one.jsonl"
    second_path = second_root / "sessions/two.jsonl"
    _write_jsonl(first_path, _codex_records("shared-id"))
    _write_jsonl(second_path, _codex_records("shared-id"))
    database = tmp_path / "archive.sqlite"
    provider = get_provider("codex")
    with Archive(database) as archive:
        first = archive.upload(root=first_root, provider=provider, transcripts=[first_path])
        second = archive.upload(root=second_root, provider=provider, transcripts=[second_path])

    assert first.imported == 1
    assert second.duplicates == 1
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT count(*) FROM locations").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM conversations").fetchone() == (1,)


def test_same_path_on_different_hosts_creates_distinct_locations(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    path = root / "sessions/session.jsonl"
    database = tmp_path / "archive.sqlite"
    provider = get_provider("codex")
    _write_jsonl(path, _codex_records("host-a-session"))
    with Archive(database, hostname="workstation-a") as archive:
        archive.upload(root=root, provider=provider, transcripts=[path])

    _write_jsonl(path, _codex_records("host-b-session"))
    with Archive(database, hostname="workstation-b") as archive:
        archive.upload(root=root, provider=provider, transcripts=[path])

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT hostname, root_path FROM locations ORDER BY hostname"
        ).fetchall() == [
            ("workstation-a", str(root.resolve())),
            ("workstation-b", str(root.resolve())),
        ]
        assert connection.execute("SELECT count(*) FROM conversations").fetchone() == (2,)


def test_changed_revision_with_same_session_id_stays_separate(tmp_path: Path) -> None:
    first_root = tmp_path / ".codex"
    second_root = tmp_path / ".codex_another"
    first_path = first_root / "sessions/one.jsonl"
    second_path = second_root / "sessions/two.jsonl"
    _write_jsonl(first_path, _codex_records("shared-id"))
    changed = _codex_records("shared-id")
    changed[-1]["payload"] = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "A different revision"}],
    }
    _write_jsonl(second_path, changed)
    database = tmp_path / "archive.sqlite"
    provider = get_provider("codex")

    with Archive(database) as archive:
        archive.upload(root=first_root, provider=provider, transcripts=[first_path])
        result = archive.upload(root=second_root, provider=provider, transcripts=[second_path])

    assert result.imported == 1
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT count(*) FROM conversations").fetchone() == (2,)


def test_claude_transcript_and_subagent_metadata(tmp_path: Path) -> None:
    root = tmp_path / ".claude"
    path = root / "projects/-work/session-parent/subagents/agent-child.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "user",
                "uuid": "message-1",
                "sessionId": "session-child",
                "parentUuid": None,
                "timestamp": "2026-07-14T11:00:00Z",
                "cwd": "/work",
                "gitBranch": "feature",
                "version": "2.0.0",
                "message": {"role": "user", "content": "Explain frobnication"},
            },
            {
                "type": "assistant",
                "uuid": "message-2",
                "sessionId": "session-child",
                "parentUuid": "message-1",
                "timestamp": "2026-07-14T11:00:01Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-test",
                    "content": [{"type": "text", "text": "Certainly."}],
                },
            },
        ],
    )
    database = tmp_path / "archive.sqlite"
    provider = detect_provider(root)

    with Archive(database) as archive:
        result = archive.upload(
            root=root,
            provider=provider,
            transcripts=provider.discover(root),
        )

    assert result.imported == 1
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            """
            SELECT external_id, conversation_kind, parent_external_id, model
            FROM conversations
            """
        ).fetchone()
        assert row == ("session-child", "subagent", "session-parent", "claude-test")
